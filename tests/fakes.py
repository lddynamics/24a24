import copy

from app.agent.llm import LLMReply, ToolCall
from app.channels.sender import SendError


def text_reply(text: str) -> LLMReply:
    return LLMReply(text=text, tool_calls=[], content_blocks=[{"type": "text", "text": text}], stop_reason="end_turn",
                    input_tokens=10, output_tokens=5)


def tool_reply(name: str, args: dict, call_id: str = "toolu_1") -> LLMReply:
    return LLMReply(
        text="",
        tool_calls=[ToolCall(call_id, name, args)],
        content_blocks=[{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=5,
    )


class FakeLLM:
    """Rejoue des réponses scriptées et mémorise chaque appel (système, outils, messages)."""

    def __init__(self, *replies, repeat_last: bool = False, error: Exception | None = None, on_first_call=None):
        self.replies = list(replies)
        self.repeat_last = repeat_last
        self.error = error
        self.calls: list[dict] = []
        self.on_first_call = on_first_call  # simule un message client arrivant PENDANT le traitement

    def create(self, *, model, system, tools, messages, max_tokens):
        self.calls.append(copy.deepcopy(dict(model=model, system=system, tools=tools, messages=messages)))
        if len(self.calls) == 1 and self.on_first_call:
            self.on_first_call()
        if self.error:
            raise self.error
        if len(self.replies) > 1 or not self.repeat_last:
            return self.replies.pop(0)
        return self.replies[0]


class FakeSender:
    def __init__(self, fail_times: int = 0, permanent: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.sent_audio: list[tuple[str, bytes, str]] = []
        self.sent_images: list[tuple[str, bytes, str]] = []
        self.log: list[str] = []                      # ordre réel des envois : "image" / "text"
        self.fail_times = fail_times
        self.permanent = permanent

    def send_audio(self, recipient_id, data, content_type):
        if self.fail_times:
            self.fail_times -= 1
            raise SendError("échec simulé", permanent=self.permanent)
        self.sent_audio.append((recipient_id, data, content_type))
        return f"mid_audio_{len(self.sent_audio)}"

    def send_image(self, recipient_id, data, content_type):
        if self.fail_times:
            self.fail_times -= 1
            raise SendError("échec simulé", permanent=self.permanent)
        self.sent_images.append((recipient_id, data, content_type))
        self.log.append("image")
        return f"mid_img_{len(self.sent_images)}"

    def send_text(self, recipient_id, text):
        if self.fail_times:
            self.fail_times -= 1
            raise SendError("échec simulé", permanent=self.permanent)
        self.log.append("text")
        self.sent.append((recipient_id, text))
        return f"mid_out_{len(self.sent)}"

    def factory(self):
        return lambda channel_type, token: self
