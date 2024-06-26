import sys
import os
sys.path.append(os.path.abspath(os.curdir))

import wandb
import numpy as np
import math
import einops
import random

from torch.cuda.amp import autocast
import torch.nn as nn
import torch.nn.functional as F
import torch

from functools import partial
from tqdm.auto import tqdm
from collections import namedtuple
from denoising_diffusion_pytorch.version import __version__

from src.components.utils.settings import Settings
from src.components.utils import functions as func
from src.components.model.unet import Unet

import logging
logging.getLogger('apscheduler.executors.default').propagate = False

# constants

ModelPrediction = namedtuple('ModelPrediction', ['pred_noise', 'pred_x0'])

SETTINGS = Settings()
LOGGER=SETTINGS.logger()

def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


def linear_beta_schedule(timesteps):
    """
    linear schedule, proposed in original ddpm paper
    """
    scale = 1000 / timesteps
    beta_start = scale * 0.0001
    beta_end = scale * 0.02
    return torch.linspace(beta_start, beta_end, timesteps, dtype=torch.float64)


def cosine_beta_schedule(timesteps, s=0.008):
    """
    cosine schedule
    as proposed in https://openreview.net/forum?id=-NEXDKk8gZ
    """
    steps = timesteps + 1
    t = torch.linspace(0, timesteps, steps, dtype=torch.float64) / timesteps
    alphas_cumprod = torch.cos((t + s) / (1 + s) * math.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0, 0.999)


def sigmoid_beta_schedule(timesteps, start=-3, end=3, tau=1, clamp_min=1e-5):
    """
    sigmoid schedule
    proposed in https://arxiv.org/abs/2212.11972 - Figure 8
    better for images > 64x64, when used during training
    """
    steps = timesteps + 1
    t = torch.linspace(0, timesteps, steps, dtype=torch.float64) / timesteps
    v_start = torch.tensor(start / tau).sigmoid()
    v_end = torch.tensor(end / tau).sigmoid()
    alphas_cumprod = (-((t * (end - start) + start) /
                      tau).sigmoid() + v_end) / (v_end - v_start)
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0, 0.999)

# Diffusion model class

class Diffusion_Model(nn.Module):
    def __init__(
        self,
        unet: Unet,
        path_save_model: str,
        device,
        image_chw,
        timesteps,  # Number of steps is typically in the order of thousands
        *,
        sampling_timesteps=None,
        objective='pred_noise',
        beta_schedule='sigmoid',
        schedule_fn_kwargs=dict(),
        ddim_sampling_eta=0.,
        auto_normalize=True,
        offset_noise_strength=0.,  # https://www.crosslabs.org/blog/diffusion-with-offset-noise
        min_snr_loss_weight=False,  # https://arxiv.org/abs/2303.09556
        min_snr_gamma=5,

    ):
        super().__init__()
        assert not (
            type(self) == Diffusion_Model and unet.channels != unet.out_dim)
        assert not unet.random_or_learned_sinusoidal_cond

        self.unet = unet.to(device=device)
        self.channels = self.unet.channels
        self.self_condition = self.unet.self_condition
        self.path_save_model = path_save_model

        self.image_channel, self.image_height, self.image_width = image_chw

        self.objective = objective

        assert objective in {'pred_noise', 'pred_x0',
                             'pred_v'}, 'objective must be either pred_noise (predict noise) or pred_x0 (predict image start) or pred_v (predict v [v-parameterization as defined in appendix D of progressive distillation paper, used in imagen-video successfully])'

        if beta_schedule == 'linear':
            beta_schedule_fn = linear_beta_schedule
        elif beta_schedule == 'cosine':
            beta_schedule_fn = cosine_beta_schedule
        elif beta_schedule == 'sigmoid':
            beta_schedule_fn = sigmoid_beta_schedule
        else:
            raise ValueError(f'unknown beta schedule {beta_schedule}')

        betas = beta_schedule_fn(timesteps, **schedule_fn_kwargs).to(device=device)

        alphas = 1. - betas
        # Returns the cumulative product of elements of input in the dimension dim.
        alphas_cumprod = torch.cumprod(alphas, dim=0)
        # Pads tensor.
        alphas_cumprod_prev = F.pad(alphas_cumprod[:-1], (1, 0), value=1.)

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)

        # sampling related parameters

        # default num sampling timesteps to number of timesteps at training
        self.sampling_timesteps = func.default(sampling_timesteps, timesteps)

        assert self.sampling_timesteps <= timesteps
        self.is_ddim_sampling = self.sampling_timesteps < timesteps
        self.ddim_sampling_eta = ddim_sampling_eta

        # helper function to register buffer from float64 to float32

        def register_buffer(name, val): return self.register_buffer(
            name, val.to(torch.float32))

        register_buffer('betas', betas)
        register_buffer('alphas_cumprod', alphas_cumprod)
        register_buffer('alphas_cumprod_prev', alphas_cumprod_prev)

        # calculations for diffusion q(x_t | x_{t-1}) and others

        register_buffer('sqrt_alphas_cumprod', torch.sqrt(alphas_cumprod))
        register_buffer('sqrt_one_minus_alphas_cumprod',
                        torch.sqrt(1. - alphas_cumprod))
        register_buffer('log_one_minus_alphas_cumprod',
                        torch.log(1. - alphas_cumprod))
        register_buffer('sqrt_recip_alphas_cumprod',
                        torch.sqrt(1. / alphas_cumprod))
        register_buffer('sqrt_recipm1_alphas_cumprod',
                        torch.sqrt(1. / alphas_cumprod - 1))

        # calculations for posterior q(x_{t-1} | x_t, x_0)

        posterior_variance = betas * \
            (1. - alphas_cumprod_prev) / (1. - alphas_cumprod)

        # above: equal to 1. / (1. / (1. - alpha_cumprod_tm1) + alpha_t / beta_t)

        register_buffer('posterior_variance', posterior_variance)

        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain

        register_buffer('posterior_log_variance_clipped',
                        torch.log(posterior_variance.clamp(min=1e-20)))
        register_buffer('posterior_mean_coef1', betas *
                        torch.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod))
        register_buffer('posterior_mean_coef2', (1. - alphas_cumprod_prev)
                        * torch.sqrt(alphas) / (1. - alphas_cumprod))

        # offset noise strength - in blogpost, they claimed 0.1 was ideal

        self.offset_noise_strength = offset_noise_strength

        # derive loss weight
        # SNR: Signal-to-Noise-Ratio

        snr = alphas_cumprod / (1 - alphas_cumprod)

        # https://arxiv.org/abs/2303.09556

        maybe_clipped_snr = snr.clone()
        if min_snr_loss_weight:
            maybe_clipped_snr.clamp_(max=min_snr_gamma)

        if objective == 'pred_noise':
            register_buffer('loss_weight', maybe_clipped_snr / snr)
        elif objective == 'pred_x0':
            register_buffer('loss_weight', maybe_clipped_snr)
        elif objective == 'pred_v':
            register_buffer('loss_weight', maybe_clipped_snr / (snr + 1))

        # auto-normalization of data [0, 1] -> [-1, 1] - can turn off by setting it to be False

        self.normalize = func.normalize_to_neg_one_to_one if auto_normalize else func.identity
        self.unnormalize = func.unnormalize_to_zero_to_one if auto_normalize else func.identity

    @property
    def device(self):
        return self.betas.device

    def predict_start_from_noise(self, x_t, t, noise):
        return (
            extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * noise
        )

    def predict_noise_from_start(self, x_t, t, x0):
        return (
            (extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - x0) /
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)
        )

    def predict_v(self, x0, t, noise):
        return (
            extract(self.sqrt_alphas_cumprod, t, x0.shape) * noise -
            extract(self.sqrt_one_minus_alphas_cumprod,
                    t, x0.shape) * x0
        )

    def predict_start_from_v(self, x_t, t, v):
        return (
            extract(self.sqrt_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_t.shape) * v
        )

    def q_posterior(self, x0, x_t, t):
        posterior_mean = (
            extract(self.posterior_mean_coef1, t, x_t.shape) * x0 +
            extract(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract(
            self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def model_predictions(self, x, t, x_self_cond=None, clip_x0=False, rederive_pred_noise=False):
        model_output = self.unet(x, t, x_self_cond)
        maybe_clip = partial(torch.clamp, min=-1., max=1.) if clip_x0 else func.identity

        if self.objective == 'pred_noise':
            pred_noise = model_output
            x0 = self.predict_start_from_noise(x, t, pred_noise)
            x0 = maybe_clip(x0)

            if clip_x0 and rederive_pred_noise:
                pred_noise = self.predict_noise_from_start(x, t, x0)

        elif self.objective == 'pred_x0':
            x0 = model_output
            x0 = maybe_clip(x0)
            pred_noise = self.predict_noise_from_start(x, t, x0)

        elif self.objective == 'pred_v':
            v = model_output
            x0 = self.predict_start_from_v(x, t, v)
            x0 = maybe_clip(x0)
            pred_noise = self.predict_noise_from_start(x, t, x0)

        return ModelPrediction(pred_noise, x0)

    def p_mean_variance(self, x, t, x_self_cond=None, clip_denoised=True):
        preds = self.model_predictions(x, t, x_self_cond)
        x0 = preds.pred_x0

        if clip_denoised:
            x0.clamp_(-1., 1.)

        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(
            x0=x0, x_t=x, t=t)
        return model_mean, posterior_variance, posterior_log_variance, x0

    @torch.inference_mode()
    def p_sample(self, x, t: int, x_self_cond=None):
        b, *_, device = *x.shape, self.device
        batched_times = torch.full((b,), t, device=device, dtype=torch.long)
        model_mean, _, model_log_variance, x0 = self.p_mean_variance(x=x, t=batched_times, 
                                                                     x_self_cond=x_self_cond, 
                                                                     clip_denoised=True)
        
        noise = torch.randn_like(x) if t > 0 else 0.  # no noise if t == 0
        pred_img = model_mean + (0.5 * model_log_variance).exp() * noise

        return pred_img, x0

    @torch.inference_mode()
    def p_sample_loop(self, shape, t_min: int, t_max: int, noise_img=None, return_all_timesteps=False):
        batch, device = shape[0], self.device

        img = func.default(noise_img, torch.randn(shape, device=device))
        imgs = [img]
            
        x0 = None

        for t in tqdm(reversed(range(t_min, t_max)), desc='Sampling loop time step of DDPM', total=(t_max - t_min)):
            self_cond = x0 if self.self_condition else None
            img, x0 = self.p_sample(img, t, self_cond)
            imgs.append(img)

        ret = img if not return_all_timesteps else torch.stack(imgs, dim=1)

        ret = self.unnormalize(ret)
        return ret

    @torch.inference_mode()
    def ddim_sample(self, shape, t_min: int, t_max: int, noise_img=None, return_all_timesteps=False):
        
        batch, device, total_timesteps, sampling_timesteps, eta, objective = \
        shape[0], self.device, self.num_timesteps, self.sampling_timesteps, self.ddim_sampling_eta, self.objective

        # [-1, 0, 1, 2, ..., T-1] when sampling_timesteps == total_timesteps
        times = torch.linspace(-1, total_timesteps - 1, steps=sampling_timesteps + 1)
        times = times[(times >= t_min) & (times < t_max)]
        times = list(reversed(times.int().tolist()))
        # [(T-1, T-2), (T-2, T-3), ..., (1, 0), (0, -1)]
        time_pairs = list(zip(times[:-1], times[1:]))

        img = func.default(noise_img, torch.randn(shape, device=device))
        imgs = [img]

        x0 = None

        for time, time_next in tqdm(time_pairs, desc='sampling loop time step of DDIM'):
            time_cond = torch.full((batch,), time, device=device, dtype=torch.long)
            self_cond = x0 if self.self_condition else None
            pred_noise, x0, *_ = self.model_predictions(
                img, time_cond, self_cond, clip_x0=True, rederive_pred_noise=True)

            if time_next < 0:
                img = x0
                imgs.append(img)
                continue

            alpha = self.alphas_cumprod[time]
            alpha_next = self.alphas_cumprod[time_next]

            sigma = eta * ((1 - alpha / alpha_next) *
                           (1 - alpha_next) / (1 - alpha)).sqrt()
            c = (1 - alpha_next - sigma ** 2).sqrt()

            noise = torch.randn_like(img)

            img = x0 * alpha_next.sqrt() + \
                c * pred_noise + \
                sigma * noise

            imgs.append(img)

        ret = img if not return_all_timesteps else torch.stack(imgs, dim=1)

        ret = self.unnormalize(ret)
        return ret

    @torch.inference_mode()
    def sample(self, t_min: int, t_max: int, batch_size=16, noise_img=None, return_all_timesteps=False): 
        sample_fn = self.p_sample_loop if not self.is_ddim_sampling else self.ddim_sample
        return sample_fn(shape=(batch_size, self.channels, self.image_height, self.image_width), 
                         t_min=t_min, t_max=t_max, noise_img=noise_img, 
                         return_all_timesteps=return_all_timesteps)

    @torch.inference_mode()
    def interpolate(self, x1, x2, t=None, lam=0.5):
        b, *_, device = *x1.shape, x1.device
        t = func.default(t, self.num_timesteps - 1)

        assert x1.shape == x2.shape

        t_batched = torch.full((b,), t, device=device)
        xt1, xt2 = map(lambda x: self.forward(x, t=t_batched), (x1, x2))

        img = (1 - lam) * xt1 + lam * xt2

        x0 = None

        for i in tqdm(reversed(range(0, t)), desc='interpolation sample time step', total=t):
            self_cond = x0 if self.self_condition else None
            img, x0 = self.p_sample(img, i, self_cond)

        return img

    @autocast(enabled=False)
    def forward(self, x0, t, noise=None, offset_noise_strength=None):

        noise = func.default(noise, lambda: torch.randn_like(x0))

        # offset noise - https://www.crosslabs.org/blog/diffusion-with-offset-noise

        offset_noise_strength = func.default(
            offset_noise_strength, self.offset_noise_strength)

        if offset_noise_strength > 0.:
            offset_noise = torch.randn(x0.shape[:2], device=self.device)
            noise += offset_noise_strength * \
                einops.rearrange(offset_noise, 'b c -> b c 1 1')

        return (
            extract(self.sqrt_alphas_cumprod, t, x0.shape) * x0 +
            extract(self.sqrt_one_minus_alphas_cumprod, t, x0.shape) * noise
        )

    def p_losses(self, x, x0, t, noise):

        # if doing self-conditioning, 50% of the time, predict x0 from current set of times
        # and condition with unet with that
        # this technique will slow down training by 25%, but seems to lower FID significantly

        x_self_cond = None
        if self.self_condition and random() < 0.5:
            with torch.inference_mode():
                x_self_cond = self.model_predictions(x, t).pred_x0
                x_self_cond.detach_()

        # predict and take gradient step

        model_out = self.unet(x, t, x_self_cond)

        if self.objective == 'pred_noise':
            target = noise
        elif self.objective == 'pred_x0':
            target = x0
        elif self.objective == 'pred_v':
            v = self.predict_v(x0, t, noise)
            target = v
        else:
            raise ValueError(f'unknown objective {self.objective}')

        loss = F.mse_loss(model_out, target, reduction='none')
        loss = einops.reduce(loss, 'b ... -> b', 'mean')

        loss = loss * extract(self.loss_weight, t, loss.shape)
        return loss.mean()

    def backward(self, noisy_imgs, imgs, t, noise, *args, **kwargs):

        b, c, h, w = noisy_imgs.shape
        assert h == self.image_height and w == self.image_width, f'height and width of image must be {self.image_height} and {self.image_width}, but are {h} and {w}'

        noisy_imgs = self.normalize(noisy_imgs)

        return self.p_losses(x=noisy_imgs, x0=imgs, t=t, noise=noise, *args, **kwargs)