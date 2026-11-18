import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}

SUMMARY_PROMPT = """Ты помогаешь быстро понять содержание голосового сообщения. Тебе дают его автоматическую расшифровку.

Сделай краткий пересказ. Его разбивают на поля, оформлять ничего не нужно — бот сам сверстает сообщение:
- gist — главная суть: одно короткое предложение, до 25 слов. Если сообщение очень короткое или простое — только gist, остальные поля пустые.
- points — 2–6 ключевых мыслей, фактов, просьб. Каждый пункт — одна мысль, до 20 слов, без вводных вроде «Также он упоминает, что». Не повторяй gist.
- tasks — только то, о чём автор прямо просит или договаривается: действия, сроки, даты, суммы, встречи, контакты. Если ничего такого нет — пустой список; не пиши пунктов вроде «Ничего не требуется» или «выслушать автора».
- Пиши на том же языке, на котором говорит автор.
- Пересказывай от третьего лица. Если известно имя автора — называй его по имени («Саша просит…»), иначе «Автор просит…». О том, кому адресовано сообщение, пиши «тебя», «тебе» («зовёт тебя на дачу»), не «получатель».
- Ничего не выдумывай и не добавляй от себя. Ошибки распознавания исправляй по смыслу, только если уверен.
- Не додумывай эмоции, мотивы и состояние автора и не давай оценок вроде «тон напряжённый» — пересказывай только сказанное.
- Если расшифровка обрывочная или непонятная — так и скажи в gist («Сообщение обрывочное: …») и перескажи в points то, что разборчиво.
- Только обычный текст: без Markdown, звёздочек, решёток, маркеров «•» и «-» в начале пунктов.

Ответь только JSON-объектом, без пояснений и без ```:
{"gist": "...", "points": ["..."], "tasks": ["..."]}"""


class OpenRouterError(RuntimeError):
    pass


@dataclass
class Transcription:
    text: str
    seconds: float
    cost: float  # USD


@dataclass
class Recap:
    gist: str
    points: list[str]
    tasks: list[str]  # requests and arrangements, empty when there are none

    @property
    def text(self) -> str:
        """Plain-text rendering, for logs and length stats."""
        return "\n".join([self.gist, *(f"• {p}" for p in self.points), *(f"• {t}" for t in self.tasks)])


@dataclass
class Summary:
    recap: Recap
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost: float  # USD


def _items(value) -> list[str]:
    """A list of non-empty strings, with any bullet the model added anyway stripped."""
    if not isinstance(value, list):
        return []
    return [t for t in (str(v).strip().lstrip("•-–—* ").strip() for v in value) if t]


def parse_summary(raw: str) -> Recap:
    """Parse the model's JSON answer; a non-JSON answer is taken as a bare gist."""
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("gist"), str):
        log.warning("Summary is not JSON: %r", raw[:200])
        return Recap(raw, [], [])
    return Recap(data["gist"].strip(), _items(data.get("points")), _items(data.get("tasks")))


class OpenRouter:
    def __init__(self, api_key: str, stt_model: str, summary_model: str, summary_reasoning: bool = False):
        self.stt_model = stt_model
        self.summary_model = summary_model
        self.summary_reasoning = summary_reasoning
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "VoiceRecap"},
            timeout=httpx.Timeout(180, connect=15),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, payload: dict, attempts: int = 4) -> dict:
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.client.post(path, json=payload)
            except httpx.TransportError as e:
                if attempt == attempts:
                    raise OpenRouterError(f"{path}: network error: {e}") from e
                log.warning("%s: network error (%s), retry %d", path, e, attempt)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" in data:
                        raise OpenRouterError(f"{path}: {data['error']}")
                    return data
                if resp.status_code not in RETRY_STATUSES or attempt == attempts:
                    raise OpenRouterError(f"{path}: HTTP {resp.status_code}: {resp.text[:500]}")
                log.warning("%s: HTTP %d, retry %d", path, resp.status_code, attempt)
            await asyncio.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    async def transcribe(self, audio: Path, language: str | None = None) -> Transcription:
        """language: ISO-639-1 code; None lets the model auto-detect."""
        payload = {
            "model": self.stt_model,
            "input_audio": {
                "data": base64.b64encode(audio.read_bytes()).decode(),
                "format": audio.suffix.lstrip(".") or "mp3",
            },
        }
        if language:
            payload["language"] = language
        data = await self._post("/audio/transcriptions", payload)
        usage = data.get("usage") or {}
        log.info("STT %s (lang=%s): usage=%s", self.stt_model, language or "auto", usage)
        return Transcription(
            text=(data.get("text") or "").strip(),
            seconds=float(usage.get("seconds") or 0),
            cost=float(usage.get("cost") or 0),
        )

    async def summarize(self, transcript: str, author: str | None = None) -> Summary:
        """author: the sender's name, if known."""
        context = f"Автор: {author}" if author else "Имя автора неизвестно"
        payload = {
            "model": self.summary_model,
            "messages": [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"{context}\n\nРасшифровка:\n\n{transcript}"},
            ],
            "temperature": 0.3,
            "max_tokens": 1500,
            "reasoning": {"enabled": self.summary_reasoning},
        }
        data = await self._post("/chat/completions", payload)
        usage = data.get("usage") or {}
        log.info("LLM %s: usage=%s", self.summary_model, usage)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise OpenRouterError(f"unexpected chat response: {str(data)[:500]}") from e
        return Summary(
            recap=parse_summary(text or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            cost=float(usage.get("cost") or 0),
        )


async def _smoke(path: str) -> None:
    """python -m bot.openrouter <audio file> — transcribe and summarize a local file."""
    import tempfile

    from bot.audio import to_mp3_chunks
    from bot.config import settings

    logging.basicConfig(level=logging.INFO)
    api = OpenRouter(settings.openrouter_api_key, settings.stt_model, settings.summary_model, settings.summary_reasoning)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            chunks = await to_mp3_chunks(Path(path), Path(tmp), settings.chunk_sec)
            parts = [await api.transcribe(c, settings.stt_language) for c in chunks]
        transcript = " ".join(p.text for p in parts)
        summary = await api.summarize(transcript)
        stt_cost = sum(p.cost for p in parts)
        print("--- transcript ---\n" + transcript)
        print("--- summary ---\n" + summary.recap.text)
        print(
            f"--- cost ---\nSTT ${stt_cost:.6f} ({sum(p.seconds for p in parts):.1f}s), "
            f"LLM ${summary.cost:.6f} ({summary.prompt_tokens} in / {summary.completion_tokens} out), "
            f"total ${stt_cost + summary.cost:.6f}"
        )
    finally:
        await api.close()


if __name__ == "__main__":
    import sys

    asyncio.run(_smoke(sys.argv[1]))
