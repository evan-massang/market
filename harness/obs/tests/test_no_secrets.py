"""C6 — secret values never reach disk.

Sets recognizable fake values for ANTHROPIC_API_KEY / OPENAI_API_KEY /
CHALLENGER_API_KEY / A_CUSTOM_TOKEN in the live environment, then routes those
values through three write paths:

  * ``on_run_start(config)`` — secrets as both secret-NAMED keys (dropped) and
    embedded in free-text config values (scrubbed),
  * ``on_llm_call(prompt/completion)`` — secrets embedded in the prompt + the
    completion (stored only as scrubbed blobs),
  * ``on_error(traceback)`` — secrets embedded in a raised exception's message
    (scrubbed in the event + the errors/ mirror).

Then byte-greps EVERY file under the temp logs dir (events/, errors/, blobs/) and
asserts ZERO occurrences of any fake secret value — while confirming the
redaction marker IS present (so the scrub demonstrably ran, not silently no-op'd)
and that real output was actually produced.
"""

from harness import obs
from harness.obs.tests._util import temp_obs_env, run_as_main

# Distinctive, len>=6, recognizable-if-leaked secret values.
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-FAKESECRET-AAAA1111",
    "OPENAI_API_KEY": "sk-openai-FAKESECRET-BBBB2222",
    "CHALLENGER_API_KEY": "ch-FAKESECRET-CCCC3333",
    "A_CUSTOM_TOKEN": "tok-FAKESECRET-DDDD4444",
}
RUN_ID = "run_c6_0001"


def _route_secrets():
    h = obs.hooks
    a = SECRETS["ANTHROPIC_API_KEY"]
    o = SECRETS["OPENAI_API_KEY"]
    c = SECRETS["CHALLENGER_API_KEY"]
    t = SECRETS["A_CUSTOM_TOKEN"]

    with obs.run_ctx(run_id=RUN_ID):
        # (1) on_run_start: secret-named keys (dropped) + free-text (scrubbed)
        h.on_run_start(
            {
                "ANTHROPIC_API_KEY": a,
                "OPENAI_API_KEY": o,
                "nested": {"CHALLENGER_API_KEY": c},
                "note": "configured with %s and %s and %s and %s" % (a, o, c, t),
            },
            1000.0,
        )
        # (2) on_llm_call: secrets in prompt + completion (scrubbed blobs)
        h.on_llm_call(
            "ollama", "qwen2.5:7b",
            "system uses key %s" % a,
            "user uses %s and %s" % (o, c),
            "completion leaks %s" % t,
            10, 5, 100.0, "agent",
        )
        # (3) on_error: secrets in the exception message (scrubbed everywhere)
        try:
            raise RuntimeError("boom leaking %s %s %s %s" % (a, o, c, t))
        except Exception as exc:
            h.on_error("test.site", exc, "continue", context={"phase": "c6"})


def _all_log_files():
    root = obs.config.LOGS_DIR()
    return [p for p in root.rglob("*") if p.is_file()]


def test_no_secrets():
    with temp_obs_env(prefix="obs_c6_", extra_env=SECRETS):
        _route_secrets()

        files = _all_log_files()
        assert files, "no log files were produced — test would be vacuous"

        secret_bytes = {name: val.encode("utf-8") for name, val in SECRETS.items()}
        leaks = []
        saw_redaction = False
        for p in files:
            data = p.read_bytes()
            if b"***REDACTED***" in data:
                saw_redaction = True
            for name, vb in secret_bytes.items():
                if vb in data:
                    leaks.append((str(p), name))

        assert not leaks, "secret values leaked to disk: %r" % (leaks,)
        # Prove redaction actually fired (and wasn't an accidental no-op).
        assert saw_redaction, "no ***REDACTED*** marker found — scrub did not run"
        # Prove the secrets really were routed through (events + blobs + errors).
        rels = {p.relative_to(obs.config.LOGS_DIR()).as_posix() for p in files}
        assert any(r.startswith("events/") for r in rels), rels
        assert any(r.startswith("blobs/") for r in rels), rels
        assert any(r.startswith("errors/") for r in rels), rels


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C6 test_no_secrets", test_no_secrets)]))
