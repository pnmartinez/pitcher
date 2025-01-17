#!/usr/bin/env python
# Pitcher v 0.5.2
# Copyright (C) 2020 Morgan Mitchell
# Based on: Physical and Behavioral Circuit Modeling of the SP-12, DT Yeh, 2007
# https://ccrma.stanford.edu/~dtyeh/sp12/yeh2007icmcsp12slides.pdf

import logging
from sys import platform, path

import numpy as np

from scipy.interpolate import interp1d
from scipy.signal import ( ellip, sosfilt, tf2sos, firwin2, decimate, resample, butter )
from scipy.spatial import cKDTree

from pydub import AudioSegment

from soundfile import write as sf_write

from librosa import load                 as librosa_load
from librosa.core import resample        as librosa_resample
from librosa.util import normalize       as librosa_normalize
from librosa.effects import time_stretch as librosa_time_stretch
# TODO: could also try pyrubberband.pyrb.time_stretch

from moogfilter.moogfilter import MoogFilter

ZOH_MULTIPLIER = 4
RESAMPLE_MULTIPLIER = 2

INPUT_SR = 96000
OUTPUT_SR = 48000

# NOTE: sp-1200 rate 26040, sp-12 rate 27500
SP_SR = 26040

OUTPUT_FILTER_TYPES = [
    'lp1', 
    'lp2', 
    'moog'
]

POSITIVE_TUNING_RATIO = 1.02930223664
NEGATIVE_TUNING_RATIOS = {
    -1: 1.05652677103003,
    -2: 1.1215356033380033,
    -3: 1.1834835840896631,
    -4: 1.253228360845465,
    -5: 1.3310440397149297,
    -6: 1.4039714929646099,
    -7: 1.5028019735639886,
    -8: 1.5766735700797954
}

log_levels = {
    'INFO':     logging.INFO,
    'DEBUG':    logging.DEBUG,
    'WARNING':  logging.WARNING,
    'ERROR':    logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

log = logging.getLogger(__name__)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter('%(levelname)-8s %(message)s'))
log.addHandler(sh)


if platform == "darwin":
    if not 'ffmpeg' in path:
        path.append('/usr/local/bin/ffmpeg')
        AudioSegment.converter = '/usr/local/bin/ffmpeg'


def calc_quantize_function(quantize_bits):
    # https://dspillustrations.com/pages/posts/misc/quantization-and-quantization-noise.html
    log.info(f'calculating quantize fn with {quantize_bits} quantize bits')
    u = 1  # max amplitude to quantize
    quantization_levels = 2 ** quantize_bits
    delta_s = 2 * u / quantization_levels  # level distance
    s_midrise = -u + delta_s / 2 + np.arange(quantization_levels) * delta_s
    s_midtread = -u + np.arange(quantization_levels) * delta_s
    log.info('done calculating quantize fn')
    return s_midrise, s_midtread


def adjust_pitch(x, st):
    log.info(f'adjusting audio pitch by {st} semitones')
    t = 0
    if (0 > st >= -8):
        t = NEGATIVE_TUNING_RATIOS[st]
    elif st > 0:
        t = POSITIVE_TUNING_RATIO ** -st
    elif st == 0:  # no change
        return x
    else: # -8 > st
        # output tuning will loses precision/accuracy the further
        # we extrapolate from the device tuning ratios
        f = interp1d(
                list(NEGATIVE_TUNING_RATIOS.keys()),
                list(NEGATIVE_TUNING_RATIOS.values()),
                fill_value='extrapolate'
        )
        t = f(st)

    n = int(np.round(len(x) * t))
    r = np.linspace(0, len(x) - 1, n).round().astype(np.int32)
    pitched = [x[r[e]] for e in range(n-1)]  # could yield instead
    pitched = np.array(pitched)
    log.info('done pitching audio')

    return pitched


def filter_input(x):
    log.info('applying anti aliasing filter')
    # NOTE: Might be able to improve accuracy in the 15 -> 20kHz range with firwin?
    #       Close already, could perfect it at some point, probably not super important now.
    f = ellip(4, 1, 72, 0.666, analog=False, output='sos')
    y = sosfilt(f, x)
    log.info('done applying anti aliasing filter')
    return y


def lp1(x, sample_rate):
    log.info(f'applying output eq filter {OUTPUT_FILTER_TYPES[0]}')
    # follows filter curve shown on slide 3
    # cutoff @ 7.5kHz
    freq = np.array([0, 6510, 8000, 10000, 11111, 13020, 15000, 17500, 20000, 24000])
    att = np.array([0, 0, -5, -10, -15, -23, -28, -35, -41, -40])
    gain = np.power(10, att/20)
    f = firwin2(45, freq, gain, fs=sample_rate, antisymmetric=False)
    sos = tf2sos(f, [1.0])
    y = sosfilt(sos, x)
    log.info('done applying output eq filter')
    return y


def lp2(x, sample_rate):
    log.info(f'applying output eq filter {OUTPUT_FILTER_TYPES[1]}')
    fc = 10000
    w = fc / (sample_rate / 2)
    sos = butter(7, w, output='sos')
    y = sosfilt(sos, x)
    log.info('done applying output eq filter')
    return y


def scipy_resample(y, input_sr, target_sr, factor):
    ''' resample from input_sr to target_sr_multiple/factor'''
    log.info(f'resampling audio to sample rate of {target_sr * factor}')
    seconds = len(y)/input_sr
    target_samples = int(seconds * (target_sr * factor)) + 1
    resampled = resample(y, target_samples)
    log.info('done resample 1/2')
    log.info(f'resampling audio to sample rate of {target_sr}')
    decimated = decimate(resampled, factor)
    log.info('done resample 2/2')
    log.info('done resampling audio')
    return decimated


def zero_order_hold(y, zoh_multiplier):
    # NOTE: could also try a freq aliased sinc filter
    log.info(f'applying zero order hold of {zoh_multiplier}')
    # intentionally oversample by repeating each sample 4 times
    zoh_applied = np.repeat(y, zoh_multiplier).astype(np.float32)
    log.info('done applying zero order hold')
    return zoh_applied


def nearest_values(x, y):
    x, y = map(np.asarray, (x, y))
    tree = cKDTree(y[:, None])
    ordered_neighbors = tree.query(x[:, None], 1)[1]
    return ordered_neighbors


def q(x, S, bits):
    # NOTE: no audible difference after audacity invert test @ 12 bits
    #       however, when plotted the scaled amplitude of quantized audio is
    #       noticeably higher than old implementation, leaving for now
    log.info(f'quantizing audio @ {bits} bits')
    y = nearest_values(x, S)
    quantized = S.flat[y].reshape(x.shape)
    log.info('done quantizing')
    return quantized


# https://stackoverflow.com/questions/53633177/how-to-read-a-mp3-audio-file-into-a-numpy-array-save-a-numpy-array-to-mp3
def write_mp3(f, x, sr):
    """numpy array to MP3"""
    channels = 2 if (x.ndim == 2 and x.shape[1] == 2) else 1
    # zoh converts to float32, when librosa normalized not selected y still within [-1,1] by here
    y = np.int16(x * 2 ** 15)
    song = AudioSegment(y.tobytes(), frame_rate=sr, sample_width=2, channels=channels)
    song.export(f, format="mp3", bitrate="320k")
    return


def process_array(
        y,
        st,
        input_filter,
        quantize, 
        time_stretch,
        output_filter,
        quantize_bits,
        custom_time_stretch,
        output_filter_type,
        moog_output_filter_cutoff
    ):

    log.info('done loading')


    if input_filter:
        y = filter_input(y)
    else:
        log.info('skipping input anti aliasing filter')

    resampled = scipy_resample(y, INPUT_SR, SP_SR, RESAMPLE_MULTIPLIER)

    if quantize:
        # TODO: expose midrise option?
        # simulate analog -> digital conversion
        midrise, midtread = calc_quantize_function(quantize_bits)
        resampled = q(resampled, midtread, quantize_bits)
    else:
        log.info('skipping quantize')

    pitched = adjust_pitch(resampled, st)

    if ((custom_time_stretch == 1.0) and (time_stretch == True)):
        # Default SP-12 timestretch inherent w/ adjust_pitch
        pass
    elif ((custom_time_stretch == 0.0) or (time_stretch == False)):
        # No timestretch (e.g. original audio length):
        rate = len(pitched) / len(resampled)
        log.info('time stretch: stretching back to original length...')
        pitched = librosa_time_stretch(pitched, rate=rate)
        pass
    else:
        # Custom timestretch
        rate = len(pitched) / len(resampled)
        log.info('time stretch: stretching back to original length...')
        pitched = librosa_time_stretch(pitched, rate=rate)
        log.info(f'running custom time stretch of rate: {custom_time_stretch}')
        pitched = librosa_time_stretch(pitched, rate=custom_time_stretch)

    # oversample again (default factor of 4) to simulate ZOH
    post_zero_order_hold = zero_order_hold(pitched, ZOH_MULTIPLIER)

    # NOTE: why use scipy above and librosa here?
    #       check git history to see if there was a note about this
    output = librosa_resample(
                np.asfortranarray(post_zero_order_hold),
                orig_sr=SP_SR * ZOH_MULTIPLIER,
                target_sr=OUTPUT_SR
            )

    if output_filter:
        if output_filter_type == OUTPUT_FILTER_TYPES[0]:
            # lp eq filter cutoff @ 7.5kHz, SP outputs 3 & 4
            output = lp1(output, OUTPUT_SR)
        elif output_filter_type == OUTPUT_FILTER_TYPES[1]:
            # lp eq filter cutoff @ 10kHz, SP outputs 5 & 6
            output = lp2(output, OUTPUT_SR)
        else:
            # moog vcf approximation, SP outputs 1 & 2 originally used for kicks
            mf = MoogFilter(sample_rate=OUTPUT_SR, cutoff=moog_output_filter_cutoff)
            output = mf.process(output)
    else:
        # unfiltered like outputs 7 & 8
        log.info('skipping output eq filter')

    return output


def write_audio(output, output_file_path, normalize_output):

    log.info(f'writing {output_file_path}, at sample rate {OUTPUT_SR} with normalize_output set to {normalize_output}')

    if normalize_output:
        output = librosa_normalize(output)

    if '.mp3' in output_file_path:
        write_mp3(output_file_path, output, OUTPUT_SR)
    elif '.wav' in output_file_path:
        sf_write(output_file_path, output, OUTPUT_SR, subtype='PCM_16')
    elif '.ogg' in output_file_path:
        sf_write(output_file_path, output, OUTPUT_SR, format='ogg', subtype='vorbis')
    elif '.flac' in output_file_path:
        sf_write(output_file_path, output, OUTPUT_SR, format='flac', subtype='PCM_16')
    else:
        log.error(f'Output file type unsupported or unrecognized, saving to {output_file_path}.wav')
        sf_write(output_file_path + '.wav', output, OUTPUT_SR, subtype='PCM_16')

    log.info(f'done writing output_file_path at: {output_file_path}')
    return


def pitch(
        st: int,
        input_file_path: str,
        output_file_path: str,
        log_level: str,
        input_filter=True,
        quantize=True,
        time_stretch=True,
        output_filter=True,
        normalize_output=False,
        quantize_bits=12,
        custom_time_stretch=1.0,
        output_filter_type=OUTPUT_FILTER_TYPES[0],
        moog_output_filter_cutoff=10000,
        force_mono=False,
        input_data=None  # allows passing an array to avoid re-processing input for output_many.py
    ):

    valid_levels = list(log_levels.keys())
    if (not log_level) or (log_level.upper() not in valid_levels):
        log.warn(f'Invalid log-level: "{log_level}", log-level set to "INFO", '
                 f'valid log levels are {valid_levels}')
        log_level = 'INFO'

    log_level = log_levels[log_level]
    log.setLevel(log_level)

    if output_filter_type not in OUTPUT_FILTER_TYPES:
        log.error(f'invalid output_filter_type {output_filter_type}, valid values are {OUTPUT_FILTER_TYPES}')
        log.error(f'using output_filter_type {OUTPUT_FILTER_TYPES[0]}')

    y = None
    if input_data is not None:
        # if provided, use already processed input file data
        y = input_data
    else:
        # otherwise process the file at intput_file_path
        log.info(f'loading: "{input_file_path}" at oversampled rate: {INPUT_SR}')
        y, s = librosa_load(input_file_path, sr=INPUT_SR, mono=force_mono)

    if y.ndim == 2:  # stereo
        y1 = y[0]
        y2 = y[1]

        log.info('processing stereo channels seperately')
        log.info('processing channel 1')
        y1 = process_array(
            y1, st, input_filter, quantize, time_stretch, output_filter, quantize_bits,
            custom_time_stretch, output_filter_type, moog_output_filter_cutoff
        )
        log.info('processing channel 2')
        y2 = process_array(
            y2, st, input_filter, quantize, time_stretch, output_filter, quantize_bits,
            custom_time_stretch, output_filter_type, moog_output_filter_cutoff
        )
        y = np.hstack((y1.reshape(-1, 1), y2.reshape(-1,1)))
        write_audio(y, output_file_path, normalize_output)
    else:  # mono
        y = process_array(
            y, st, input_filter, quantize, time_stretch, output_filter, quantize_bits,
            custom_time_stretch, output_filter_type, moog_output_filter_cutoff
        )
        write_audio(y, output_file_path, normalize_output)
