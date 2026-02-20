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


def _chunk_audio(audio_path: Path, chunk_duration_ms: int = 600_000) -> list[Path]:
    """
    Split audio into chunks if it exceeds the file size limit.
    Returns list of chunk file paths.
    """
    file_size_mb = audio_path.stat().st_size / (1024 * 1024)

    if file_size_mb <= MAX_FILE_SIZE_MB:
        return [audio_path]

    audio = AudioSegment.from_file(str(audio_path))
    total_ms = len(audio)
    chunks = []

    for i, start_ms in enumerate(range(0, total_ms, chunk_duration_ms)):
        end_ms = min(start_ms + chunk_duration_ms, total_ms)
        chunk = audio[start_ms:end_ms]

        chunk_path = audio_path.parent / f"{audio_path.stem}_chunk{i:02d}.mp3"
        chunk.export(str(chunk_path), format="mp3", bitrate="128k")
        chunks.append(chunk_path)

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
                if language:
                    api_kwargs["language"] = language

                response = client.audio.transcriptions.create(**api_kwargs)

                # Parse response based on format
                if diarize and hasattr(response, "speakers"):
                    # Diarized response has speakers[] with segments
                    full_text_parts = []
                    for speaker_data in response.speakers:
                        speaker_label = speaker_data.get("speaker", "Speaker")
                        for seg in speaker_data.get("segments", []):
                            text_seg = seg.get("text", "").strip()
                            full_text_parts.append(f"{speaker_label}: {text_seg}")
                            all_segments.append({
                                "speaker": speaker_label,
                                "start": seg.get("start", 0) + time_offset,
                                "end": seg.get("end", 0) + time_offset,
                                "text": text_seg,
                            })
                    all_text.append(" ".join(full_text_parts))
                else:
                    # Standard response
                    all_text.append(response.text)

                    if hasattr(response, "segments") and response.segments:
                        for seg in response.segments:
                            all_segments.append({
                                "start": seg.get("start", seg.get("t0", 0)) + time_offset,
                                "end": seg.get("end", seg.get("t1", 0)) + time_offset,
                                "text": seg.get("text", ""),
                            })

                # Update time offset for next chunk
                if hasattr(response, "duration"):
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
    full_text = " ".join(all_text).strip()

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
