# PDF to Audio Feature Guide

This guide explains the architecture, flow, and best practices for the PDF-to-Audio presentation feature in MinuteAI.

---

## 🚀 Feature Overview
The PDF to Audio feature converts raw academic or business documents (PDFs) into natural, engaging spoken presentations (MP3). Users can specify:
- **Purpose**: Thesis defense, class presentation, startup pitch, podcast explainer, study review, etc.
- **Duration**: Target speech length from 1 to 60 minutes.
- **Voice**: Gender options (male/female) using Deepgram's high-quality Aura TTS models.

---

## 🛠️ Architecture & Flow

```
[ Upload PDF ] 
      │
      ▼
1. Extract Text (pdfplumber)
      │
      ▼
2. Write Presentation Script (Claude Sonnet)
      │
      ▼
3. Chunk Script (split_text_into_chunks)
      │
      ▼
4. Generate Speech (Deepgram Aura TTS) ──> Sequential requests (each < 2000 chars)
      │
      ▼
5. Merge Audio (b"".join(mp3_chunks))
      │
      ▼
[ Downloadable MP3 ]
```

### 1. Text Extraction
- **Tool**: `pdfplumber`
- **Location**: `extract_pdf_text()` in `backend/services/pdf_audio.py`
- **Behavior**: Reads bytes, extracts clean text page-by-page. Scanned PDF images raise an error advising the user to upload a text-based document.

### 2. Speech Script Generation
- **Tool**: `Claude` via Anthropic API
- **Location**: `generate_script()` in `backend/services/pdf_audio.py`
- **Behavior**: Claude acts as a professional speech coach, converting dry source text into first-person spoken words tailored to the selected target duration (calculated at 130 words per minute).

### 3. Sentence-level Chunking & TTS
- **Tool**: `Deepgram Aura TTS` (via `/v1/speak`)
- **Location**: `split_text_into_chunks()` and `text_to_speech()` in `backend/services/pdf_audio.py`
- **Behavior**: 
  - Deepgram has a hard character limit of **2000 characters** per request.
  - The text is chunked into <= 1900 character segments, ensuring sentences are not cut off mid-word or mid-phrase.
  - API calls are executed sequentially, returning binary MP3 frame chunks.

### 4. Audio Merging
- **Behavior**: Because MP3 files are comprised of standalone audio frames, they can be natively merged by concatenating their binary bytes (`b"".join(chunks)`). The output is a single, seamless, continuous MP3 file.

---

## 💡 Recommended Best Practices & Improvements

To take this feature to the next level, implement these enhancements:

### 1. Increase the PDF Source Limit (Context Window)
- **Current Approach**: The script truncates source text to the first 16,000 characters to avoid token bloating.
- **Best Approach**: Since Claude models support 200k+ tokens, remove this hard limit or increase it to **500,000 characters**. This allows processing of full books or long research papers.

### 2. Implement Multi-Stage Summarization for Long PDFs
- For files over 50 pages:
  1. Use Claude to extract key findings and generate an executive outline.
  2. Feed the executive outline to the script-writing prompt. 
  This prevents Claude from getting lost in details and produces a more cohesive presentation.

### 3. Parallelize TTS Requests
- **Current Approach**: Requests are sent sequentially, which takes ~4–8 seconds for long scripts.
- **Best Approach**: Use Python's `concurrent.futures.ThreadPoolExecutor` or `asyncio` to fetch Deepgram audio chunks in parallel, then sort them by index and join them. This reduces TTS conversion time to under 1 second.

### 4. Asynchronous Task Queue
- **Current Approach**: Runs using FastAPI's `BackgroundTasks` in memory.
- **Best Approach**: For production scale, move PDF processing to a distributed task queue like **Celery** (with Redis/RabbitMQ) to handle longer timeouts, retries, and prevent API container crashes.
