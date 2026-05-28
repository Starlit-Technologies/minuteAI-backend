"""Speaker voice profile service.

First-call flow:
  1. Host opens "Name Speakers" modal → sees attendees as quick-select suggestions
  2. Host assigns Speaker A → Alice, Speaker B → Bob → saves
  3. Backend upserts voice_profiles (name + workspace) immediately
  4. Background thread extracts Resemblyzer embeddings from the recording if available

Subsequent-call flow:
  1. New meeting transcribed → auto_suggest_speakers() called
  2. If embeddings stored: cosine-compare speaker audio → assign if sim >= 0.80
  3. Fallback: attendee list × known profile names → medium-confidence suggestions
"""

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.80

try:
    import imageio_ffmpeg as _iio_ffmpeg
    _FFMPEG = _iio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG = "ffmpeg"


# ── Embedding helpers ─────────────────────────────────────────────────────────

def _cosine_sim(a: list, b: list) -> float:
    try:
        import numpy as np
        a, b = np.array(a, dtype=float), np.array(b, dtype=float)
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(np.dot(a, b) / denom) if denom > 0 else 0.0
    except Exception:
        return 0.0


def _extract_speaker_audio(audio_path: str, segments: list[dict], max_segs: int = 8):
    """Concat audio clips for the given segments using ffmpeg. Returns numpy array or None."""
    import subprocess
    import tempfile
    import numpy as np

    clips = []
    for seg in segments[:max_segs]:
        start_raw = float(seg.get("start") or 0)
        end_raw   = float(seg.get("end")   or 0)
        # AssemblyAI uses milliseconds (values > 1000); Deepgram uses seconds
        if start_raw > 1000:
            start_s    = start_raw / 1000
            duration_s = (end_raw - start_raw) / 1000
        else:
            start_s    = start_raw
            duration_s = end_raw - start_raw
        if duration_s < 0.5:
            continue
        tmp = None
        try:
            import soundfile as sf
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            subprocess.run(
                [_FFMPEG, "-y", "-i", audio_path,
                 "-ss", str(start_s), "-t", str(min(duration_s, 10)),
                 "-ar", "16000", "-ac", "1", "-f", "wav", tmp],
                capture_output=True, check=True, timeout=15,
            )
            data, _ = sf.read(tmp)
            if len(data) > 800:
                clips.append(data.astype(np.float32))
        except Exception:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    if not clips:
        return None
    import numpy as np
    return np.concatenate(clips)


def extract_embeddings_from_recording(
    audio_path: str,
    segments: list[dict],
) -> dict[str, list[float]]:
    """Return {speaker_label: embedding_vector}. Empty dict if Resemblyzer not installed."""
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
    except ImportError:
        log.debug("resemblyzer not installed — voice embedding extraction skipped")
        return {}

    encoder = VoiceEncoder()

    # Group segments by speaker label
    by_speaker: dict[str, list] = {}
    for seg in segments:
        spk = seg.get("speaker")
        if spk:
            by_speaker.setdefault(spk, []).append(seg)

    result: dict[str, list] = {}
    for spk, segs in by_speaker.items():
        raw = _extract_speaker_audio(audio_path, segs)
        if raw is None:
            continue
        try:
            wav = preprocess_wav(raw)
            if len(wav) < 16000:   # need at least 1 second
                continue
            emb = encoder.embed_utterance(wav)
            result[spk] = emb.tolist()
        except Exception as exc:
            log.debug("Embedding extraction failed for %s: %s", spk, exc)

    return result


# ── Profile management ────────────────────────────────────────────────────────

def update_profiles_from_assignment(
    workspace_id: str,
    speaker_map: dict[str, str],      # {"Speaker A": "Alice Chen", ...}
    audio_path: Optional[str],
    segments: Optional[list[dict]],
) -> None:
    """
    Called after host saves speaker assignments.
    Upserts name records immediately; extracts embeddings in a background thread.
    """
    from models.voice_profile import upsert_profile

    for name in speaker_map.values():
        if name and name.strip():
            upsert_profile(workspace_id, name.strip())

    if not audio_path or not segments:
        return

    def _bg():
        try:
            embeddings = extract_embeddings_from_recording(audio_path, segments)
            for label, name in speaker_map.items():
                if name and name.strip() and label in embeddings:
                    upsert_profile(workspace_id, name.strip(), embedding=embeddings[label])
            if embeddings:
                log.info("Voice profiles updated for workspace %s (%d speakers embedded)", workspace_id, len(embeddings))
        except Exception as exc:
            log.error("Background voice profile update failed: %s", exc)

    threading.Thread(target=_bg, daemon=True, name="voice-profile-update").start()


# ── Auto-suggestion ───────────────────────────────────────────────────────────

def auto_suggest_speakers(
    workspace_id: str,
    speaker_labels: list[str],
    attendees: list[str],
    audio_path: Optional[str],
    segments: Optional[list[dict]],
) -> dict[str, dict]:
    """
    Return best-guess assignments per speaker label.

    Result shape:
      {"Speaker A": {"name": "Alice Chen", "confidence": 0.91, "source": "voice_profile"}}

    Sources:
      "voice_profile"    — cosine similarity >= 0.80 against stored embedding
      "attendee_profile" — person is a known profile AND is in the invite
      "attendee"         — person is in the invite but has no stored profile yet
    """
    from models.voice_profile import get_profiles

    profiles    = get_profiles(workspace_id)
    result: dict[str, dict] = {}
    assigned_names: set[str] = set()

    # ── Step 1: Embedding-based matching ─────────────────────────────────────
    if audio_path and segments:
        profiled = [p for p in profiles if p["embedding"]]
        if profiled:
            try:
                embeddings = extract_embeddings_from_recording(audio_path, segments)
                for label in speaker_labels:
                    if label not in embeddings:
                        continue
                    best_name, best_sim = None, 0.0
                    for p in profiled:
                        if p["person_name"] in assigned_names:
                            continue
                        sim = _cosine_sim(embeddings[label], p["embedding"])
                        if sim > best_sim:
                            best_sim, best_name = sim, p["person_name"]
                    if best_sim >= _MATCH_THRESHOLD and best_name:
                        result[label] = {
                            "name":       best_name,
                            "confidence": round(best_sim, 3),
                            "source":     "voice_profile",
                        }
                        assigned_names.add(best_name)
            except Exception as exc:
                log.debug("Voice matching failed: %s", exc)

    # ── Step 2: Attendee × known-profile heuristic ────────────────────────────
    known_names    = {p["person_name"] for p in profiles}
    known_emails   = {p["person_email"] for p in profiles if p["person_email"]}
    # Attendees may be names or email addresses
    matched_known  = [
        a for a in attendees
        if a in known_names or a in known_emails
    ]
    unmatched_attendees = [a for a in attendees if a not in matched_known]
    unassigned = [lbl for lbl in speaker_labels if lbl not in result]

    for i, label in enumerate(unassigned):
        if i < len(matched_known):
            name = matched_known[i]
            result[label] = {"name": name, "confidence": 0.55, "source": "attendee_profile"}
            assigned_names.add(name)
        elif i - len(matched_known) < len(unmatched_attendees):
            name = unmatched_attendees[i - len(matched_known)]
            result[label] = {"name": name, "confidence": 0.35, "source": "attendee"}
            assigned_names.add(name)

    return result
