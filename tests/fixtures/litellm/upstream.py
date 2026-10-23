"""An isolated Anthropic HTTP peer; it never contacts a provider."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from threading import Lock

LOCK = Lock()
STATE = {"requests": 0, "drop_next": False}
USAGE = {
    "input_tokens": 10,
    "output_tokens": 4,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        with LOCK:
            self.reply({"requests": STATE["requests"]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/__control/drop-next":
            with LOCK:
                STATE["drop_next"] = True
            self.reply({"armed": True})
            return
        if self.path.endswith("count_tokens"):
            self.reply({"input_tokens": 10})
            return
        with LOCK:
            STATE["requests"] += 1
            drop = STATE["drop_next"]
            STATE["drop_next"] = False
        if drop:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        message = {
            "id": "msg_synthetic",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {**USAGE, "output_tokens": 0},
        }
        if not data.get("stream"):
            self.reply(
                {
                    **message,
                    "usage": USAGE,
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "safe"}],
                }
            )
            return
        events = [
            ("message_start", {"message": message}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "safe"}}),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {}),
        ]
        raw = "".join(
            "event: " + name + "\ndata: " + json.dumps({"type": name, **event}) + "\n\n"
            for name, event in events
        ).encode()
        self.reply(raw, "text/event-stream")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 41882), Handler).serve_forever()
