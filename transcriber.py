from __future__ import annotations
"""
Transcriber — sends audio to OpenAI GPT-4o Transcribe API.

Handles file size limits by chunking, supports speaker diarization,
and returns structured transcript with speaker labels.
"""
import json
import math
from pathlib import Path
from openai import OpenAI
from pydub import AudioSegment

from config import (
    OPENAI_API_KEY,
    TRANSCRIPTS_DIR,
    MAX_FILE_SIZE_MB,
    CHUNK_DURATION_SEC,
)

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _convert_to_mp3(wav_path: Path) -> Path:
    """Convert WAV to MP3 for smaller file size."""
    mp3_path = wav_path.with_suffix(".mp3")
    audio = AudioSegment.from_wav(str(wav_path))
    audio.export(str(mp3_path), format="mp3", bitrate="128k")
    return mp3_path


MAX_DURATION_SEC = 1300  # Stay safely under the 1400s API model limit


def _chunk_audio(audio_path: Path, chunk_duration_ms: int = 600_000) -> list[Path]:
    """
    Split audio into chunks if it exceeds file size OR duration limits.
    - File size limit: MAX_FILE_SIZE_MB (25 MB)
    - Duration limit: MAX_DURATION_SEC (1300s, safely under API's 1400s cap)
    Returns list of chunk file paths.
    """
    file_size_mb = audio_path.stat().st_size / (1024 * 1024)
    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    total_sec = total_ms / 1000.0

    needs_chunking = file_size_mb > MAX_FILE_SIZE_MB or total_sec > MAX_DURATION_SEC

    if not needs_chunking:
        return [audio_path]

    # Use the smaller of file-size-based chunk duration and duration-limit chunk
    max_chunk_ms = int(MAX_DURATION_SEC * 1000)  # duration-based cap
    effective_chunk_ms = min(chunk_duration_ms, max_chunk_ms)

    chunks = []
    for i, start_ms in enumerate(range(0, total_ms, effective_chunk_ms)):
        end_ms = min(start_ms + effective_chunk_ms, total_ms)
        chunk = audio[start_ms:end_ms]

        chunk_path = audio_path.parent / f"{audio_path.stem}_chunk{i:02d}.mp3"
        chunk.export(str(chunk_path), format="mp3", bitrate="128k")
        chunks.append(chunk_path)

    print(f"📦 Split audio into {len(chunks)} chunks "
          f"(total: {total_sec:.0f}s, {file_size_mb:.1f}MB, "
          f"chunk max: {effective_chunk_ms/1000:.0f}s)")

    return chunks


def transcribe_audio(
    audio_path: Path | str,
    language: str | None = None,
    diarize: bool = True,
    on_progress=None,
) -> dict:
    """
    Transcribe audio file using OpenAI GPT-4o Transcribe API.

    Handles:
    - WAV to MP3 conversion for smaller upload size
    - Chunking for files > 25MB
    - Speaker diarization via gpt-4o-transcribe-diarize model

    Returns dict with:
    - "text": full transcript text
    - "segments": list of {speaker, start, end, text} dicts
    - "duration_seconds": total audio duration
    """
    from pathlib import Path
    audio_path = Path(audio_path)
    client = _get_client()

    # Choose model — diarization requires a separate model
    if diarize:
        model = "gpt-4o-transcribe-diarize"
        response_format = "diarized_json"
    else:
        model = "gpt-4o-transcribe"
        response_format = "verbose_json"

    # Convert to MP3 if WAV
    if audio_path.suffix.lower() == ".wav":
        if on_progress:
            on_progress("Converting audio...")
        mp3_path = _convert_to_mp3(audio_path)
    else:
        mp3_path = audio_path

    # Chunk if necessary
    chunks = _chunk_audio(mp3_path)
    total_chunks = len(chunks)

    all_text = []
    all_segments = []
    time_offset = 0.0

    for i, chunk_path in enumerate(chunks):
        if on_progress:
            status = f"Transcribing" + (f" ({i+1}/{total_chunks})" if total_chunks > 1 else "")
            on_progress(status)

        with open(chunk_path, "rb") as f:
            try:
                # Build API kwargs — only include language if explicitly set
                # When language is None, OpenAI auto-detects (supports EN, DE, ES, etc.)
                api_kwargs = {
                    "model": model,
                    "file": f,
                    "response_format": response_format,
                }
                if diarize:
                    api_kwargs["chunking_strategy"] = {"type": "auto"}
                if language:
                    api_kwargs["language"] = language

                response = client.audio.transcriptions.create(**api_kwargs)

                # Parse response based on format. SDK objects expose attributes;
                # older responses were dicts — support both.
                def _f(obj, key, default=None):
                    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

                segments = _f(response, "segments") or []
                if diarize and segments:
                    lines = []
                    for seg in segments:
                        speaker = str(_f(seg, "speaker", "") or "Speaker")
                        text_seg = (_f(seg, "text", "") or "").strip()
                        if not text_seg:
                            continue
                        if lines and lines[-1][0] == speaker:
                            lines[-1][1].append(text_seg)
                        else:
                            lines.append((speaker, [text_seg]))
                        all_segments.append({
                            "speaker": speaker,
                            "start": (_f(seg, "start", 0) or 0) + time_offset,
                            "end": (_f(seg, "end", 0) or 0) + time_offset,
                            "text": text_seg,
                        })
                    all_text.append("\n".join(f"Speaker {sp}: {' '.join(t)}" for sp, t in lines))
                else:
                    all_text.append(_f(response, "text", "") or "")
                    for seg in segments:
                        all_segments.append({
                            "start": (_f(seg, "start", 0) or 0) + time_offset,
                            "end": (_f(seg, "end", 0) or 0) + time_offset,
                            "text": _f(seg, "text", "") or "",
                        })

                # Update time offset for next chunk
                if getattr(response, "duration", None):
                    time_offset += response.duration
                else:
                    # Estimate from chunk duration
                    chunk_audio = AudioSegment.from_file(str(chunk_path))
                    time_offset += len(chunk_audio) / 1000.0

            except Exception as e:
                print(f"⚠ Transcription error on chunk {i}: {e}")
                # Fallback to basic gpt-4o-transcribe with text format
                f.seek(0)
                response = client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=f,
                    response_format="text",
                )
                all_text.append(response if isinstance(response, str) else response.text)

        # Cleanup chunk files
        if total_chunks > 1 and chunk_path != mp3_path:
            chunk_path.unlink(missing_ok=True)

    # Build result
    full_text = "\n".join(t for t in all_text if t).strip()

    result = {
        "text": full_text,
        "segments": all_segments,
        "duration_seconds": time_offset,
    }

    # Save transcript
    transcript_path = TRANSCRIPTS_DIR / f"{audio_path.stem}_transcript.json"
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if on_progress:
        on_progress("Transcription complete")

    # Cleanup MP3
    if mp3_path != audio_path:
        mp3_path.unlink(missing_ok=True)

    return result


def format_transcript_as_markdown(transcript: dict) -> str:
    """Convert transcript dict to readable markdown."""
    text = transcript.get("text", "")
    segments = transcript.get("segments", [])
    duration = transcript.get("duration_seconds", 0)

    lines = []
    lines.append(f"## Meeting Transcript")
    lines.append(f"*Duration: {int(duration // 60)}m {int(duration % 60)}s*\n")

    if segments:
        for seg in segments:
            start = seg.get("start", 0)
            speaker = seg.get("speaker", "")
            text_seg = seg.get("text", "").strip()
            timestamp = f"[{int(start // 60):02d}:{int(start % 60):02d}]"

            if speaker:
                lines.append(f"**{speaker}** {timestamp}: {text_seg}")
            else:
                lines.append(f"{timestamp} {text_seg}")
    else:
        # No segments — just use the full text, break into paragraphs
        paragraphs = text.split(". ")
        current_paragraph = []
        for sentence in paragraphs:
            current_paragraph.append(sentence.strip())
            if len(". ".join(current_paragraph)) > 300:
                lines.append(". ".join(current_paragraph) + ".")
                lines.append("")
                current_paragraph = []
        if current_paragraph:
            lines.append(". ".join(current_paragraph))

    return "\n".join(lines)
