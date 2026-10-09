"""ACQ-STT-1: WER battery — transcribe fixed fixtures through faster-whisper.

Bar: WER < 20% per fixture (and mean). No-fabrication: silence must not
produce a hallucinated transcript. The transcript must come from the model
running on the fixture — nothing is hard-coded.
"""

from __future__ import annotations

import json
import os
import re
import sys


def _norm(text: str) -> list:
    text = text.lower()
    text = re.sub(r"[^a-z0-9'\s]", " ", text)
    return [w for w in text.split() if w]


def wer(reference: str, hypothesis: str) -> float:
    r, h = _norm(reference), _norm(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    # word-level Levenshtein
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[len(h)] / len(r)


def transcribe_all(model_dir: str, fixtures_dir: str) -> dict:
    from faster_whisper import WhisperModel
    model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    # Admitted-path configuration: vad_filter=True silences the
    # silence-hallucination found in the unfiltered run ("You" on 5s of
    # digital silence). The VAD model (silero_vad_v6.onnx) ships inside
    # the verified faster-whisper wheel — no extra acquisition.
    vad = {"vad_filter": True}
    manifest = json.load(
        open(os.path.join(fixtures_dir, "manifest.json")))
    results = []
    for item in manifest:
        segments, info = model.transcribe(item["file"], language="en",
                                          beam_size=5, **vad)
        hyp = " ".join(s.text for s in segments).strip()
        w = wer(item["transcript"], hyp)
        results.append({
            "file": os.path.basename(item["file"]),
            "reference": item["transcript"],
            "hypothesis": hyp,
            "wer": round(w, 4),
            "detected_language": info.language,
            "language_probability": round(info.language_probability, 4),
        })
        print(f'{os.path.basename(item["file"])}: WER={w:.3f} '
              f'lang={info.language}({info.language_probability:.2f})')
        print(f'  hyp: {hyp!r}')
    # No-fabrication check: digital silence must not hallucinate words.
    segments, _ = model.transcribe(
        os.path.join(fixtures_dir, "silence.wav"), language="en", beam_size=5,
        **vad)
    sil_hyp = " ".join(s.text for s in segments).strip()
    sil_words = _norm(sil_hyp)
    print(f'silence: {len(sil_words)} words: {sil_hyp!r}')
    return {"fixtures": results, "silence_hypothesis": sil_hyp,
            "silence_words": sil_words}


def main() -> int:
    model_dir = sys.argv[1]
    fixtures_dir = sys.argv[2]
    out_path = sys.argv[3]
    res = transcribe_all(model_dir, fixtures_dir)
    wers = [f["wer"] for f in res["fixtures"]]
    mean_wer = sum(wers) / len(wers)
    res["mean_wer"] = round(mean_wer, 4)
    res["max_wer"] = round(max(wers), 4)
    res["bar"] = "WER < 0.20 per fixture"
    res["pass"] = all(w < 0.20 for w in wers) and not res["silence_words"]
    with open(out_path, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f'mean WER={mean_wer:.3f} max WER={max(wers):.3f} '
          f'silence_words={len(res["silence_words"])} '
          f'-> {"PASS" if res["pass"] else "FAIL"}')
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
