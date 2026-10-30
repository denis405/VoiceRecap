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

SUMMARY_PROMPT = """Ты помогаешь быстро понять содержание голосового сообщения и ответить на него. Тебе дают его автоматическую расшифровку.

1. Сделай краткий пересказ (поле summary):
- Пиши на том же языке, на котором говорит автор.
- Начни с 1–2 предложений — главная суть.
- Затем 3–7 пунктов, каждый с новой строки и символа «• », — ключевые мысли, факты, просьбы.
- Если есть задачи, договорённости, сроки, даты, суммы, имена или контакты — добавь блок «Задачи и договорённости:» (или аналог на языке автора) с пунктами «• ».
- Если сообщение очень короткое или простое — хватит одного предложения, без пунктов.
- Пересказывай от третьего лица. Если известно имя автора — называй его по имени («Саша просит…»), иначе «Автор просит…». О том, кому адресовано сообщение, пиши «тебя», «тебе» («зовёт тебя на дачу»), не «получатель».
- Ничего не выдумывай и не добавляй от себя. Ошибки распознавания исправляй по смыслу, только если уверен.
- Не додумывай эмоции, мотивы и состояние автора и не давай оценок вроде «тон напряжённый» — пересказывай только сказанное.
- Блок задач — только если автор сам о чём-то просит или договаривается; не придумывай задачи вроде «выслушать автора».
- Если расшифровка обрывочная или непонятная — так и скажи одной фразой («Сообщение обрывочное: …») и перескажи то, что разборчиво.
- Только обычный текст: без Markdown, без звёздочек, решёток и заголовков.

2. Если тебя просят варианты ответа — предложи 2–3 готовых ответа (поле replies), которые получатель может сразу скопировать и отправить автору:
- Выбери самые уместные для этого сообщения: согласие, отказ, уточняющий вопрос, «сделаю позже», поддержка, «принял» и т. п. Один из вариантов — нейтральный, ни к чему не обязывающий.
- Пиши от первого лица получателя, на языке автора, с тем же «ты» или «вы» и в том же тоне.
- Это продолжение уже идущей переписки: без приветствий, без обращения по имени в начале, без подписи и без «спасибо за сообщение». Поздороваться можно, только если автор сам начал с приветствия.
- Как в мессенджере: 1–2 короткие фразы, не длиннее 200 символов, без канцелярита.
- Не драматизируй: не предполагай кризис, болезнь, опасность или конфликт, если автор прямо об этом не говорит. Отвечай на сказанное, а не на догадки.
- Если сообщение непонятное или обрывочное — один из вариантов: просто переспросить («Не очень понял, о чём ты, можешь пояснить?»).
- Не выдумывай факты, которых получатель не сообщал (время, суммы, планы), и не ставь заглушки вроде [время]. Если для ответа нужна такая информация — сформулируй так, чтобы она не требовалась, или задай вопрос.
- label — заголовок варианта: эмодзи и суть в 1–3 словах, на языке ответа: «👍 Да, приеду», «❓ Уточнить», «⏳ Отложить», «🙏 Принял».
- Если отвечать не на что (рассылка, монолог, заметка) — оставь replies пустым.
Если варианты ответа не нужны — replies всегда пустой.

Ответь только JSON-объектом, без пояснений и без ```:
{"summary": "...", "replies": [{"label": "...", "text": "..."}]}"""


class OpenRouterError(RuntimeError):
    pass


REPLY_TEXT_LIMIT = 300  # a reply is a short messenger message, not an essay
REPLY_LABEL_LIMIT = 24
MAX_REPLIES = 3


@dataclass
class Transcription:
    text: str
    seconds: float
    cost: float  # USD


@dataclass
class Reply:
    label: str  # short button caption
    text: str  # the reply itself, copied by the button


@dataclass
class Summary:
    text: str
    replies: list[Reply]
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost: float  # USD


def parse_summary(raw: str) -> tuple[str, list[Reply]]:
    """Split the model's JSON answer into summary and replies; a non-JSON answer is taken as a bare summary."""
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        log.warning("Summary is not JSON, replies dropped: %r", raw[:200])
        return raw, []

    replies = []
    for item in data.get("replies") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        text = str(item.get("text") or "").strip()
        if not label or not text or len(text) > REPLY_TEXT_LIMIT:
            continue
        if len(label) > REPLY_LABEL_LIMIT:
            label = label[: REPLY_LABEL_LIMIT - 1].rstrip() + "…"
        replies.append(Reply(label, text))
    return data["summary"].strip(), replies[:MAX_REPLIES]


class OpenRouter:
    def __init__(self, api_key: str, stt_model: str, summary_model: str):
        self.stt_model = stt_model
        self.summary_model = summary_model
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

    async def summarize(self, transcript: str, author: str | None = None, want_replies: bool = False) -> Summary:
        """author: the sender's name, if known; want_replies: also suggest ready-to-send replies."""
        context = [f"Автор: {author}" if author else "Имя автора неизвестно"]
        context.append("Варианты ответа: нужны" if want_replies else "Варианты ответа: не нужны")
        payload = {
            "model": self.summary_model,
            "messages": [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": "\n".join(context) + f"\n\nРасшифровка:\n\n{transcript}"},
            ],
            "temperature": 0.3,
            "max_tokens": 1500,
        }
        data = await self._post("/chat/completions", payload)
        usage = data.get("usage") or {}
        log.info("LLM %s: usage=%s", self.summary_model, usage)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise OpenRouterError(f"unexpected chat response: {str(data)[:500]}") from e
        summary, replies = parse_summary(text or "")
        return Summary(
            text=summary,
            replies=replies if want_replies else [],
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
    api = OpenRouter(settings.openrouter_api_key, settings.stt_model, settings.summary_model)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            chunks = await to_mp3_chunks(Path(path), Path(tmp), settings.chunk_sec)
            parts = [await api.transcribe(c, settings.stt_language) for c in chunks]
        transcript = " ".join(p.text for p in parts)
        summary = await api.summarize(transcript, want_replies=True)
        stt_cost = sum(p.cost for p in parts)
        print("--- transcript ---\n" + transcript)
        print("--- summary ---\n" + summary.text)
        print("--- replies ---\n" + "\n".join(f"[{r.label}] {r.text}" for r in summary.replies))
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
