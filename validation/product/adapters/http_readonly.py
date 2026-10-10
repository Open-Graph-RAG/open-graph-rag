"""Reserved adapter slot. Direct provider calls are intentionally disabled.

A live validation adapter must wire the real read-only LightRAG/MCP retrieval
and the identical candidate/baseline generation path before paid calls are enabled.
Use `replay` to score transparently captured, redacted system outputs meanwhile.
"""

class HttpAdapter:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("live HTTP adapter disabled: LightRAG/MCP A/B wiring is not implemented; use replay")
