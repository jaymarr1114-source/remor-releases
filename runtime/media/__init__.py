"""REMOR media generation substrate: audio (music, voice, song assembly)
and images (procedural generative art, see image.py).

Every sample is computed in-process with numpy/scipy. No bundled audio
samples, no loops loaded from files, no network calls. Image pixels are
likewise computed in-process with numpy and written with PIL; no learned
model, no cloud, no stock imagery.

Video: procedural animation (see runtime.media.video) -- every pixel
computed with numpy/PIL, encoded to H.264 by ffmpeg; no learned model.
"""
