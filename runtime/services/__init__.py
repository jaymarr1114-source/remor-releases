"""Operational services: HTTP adoption layer, third-track services, honest refusals.

Engine front + third-track services (unified http_adapter):
services/http_adapter.py -- one stdlib HTTP server over both route groups:
  engine front (intent dispatch, capabilities, dispatches, dispatch
  evidence, governed restore, voice/media status) and third-track
  services (run queue, projects, files, artifacts). serve()/close_services()
  are the background-thread contract; run()/_Service/_Handler the
  single-threaded engine contract (KD-2 thread-affinity).
services/run_control.py  -- cooperative pause/resume/stop via checkpoints.
services/scheduler.py    -- run queue / single-worker FIFO scheduler
  (engine boots on the worker thread; KD-2 mitigation preserved).
services/projects.py     -- project persistence, listing, monitoring API.
services/files.py        -- scoped, traversal-safe file access.
services/artifacts.py    -- artifact store: save/load/revisions + sandboxed
  execution.

Honest capability refusals:
services/unavailable.py -- CapabilityUnavailable: the honest refusal type.
services/voice.py       -- STT/TTS entry points (UNAVAILABLE: no substrate).
services/media.py       -- image/video generation entry points: the media
  substrate has landed (runtime/media/); image and video admit and
  generate where their substrate is present, and stay honestly
  fail-closed (medium_not_admitted) where it is not.
"""
