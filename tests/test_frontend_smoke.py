"""Lightweight smoke test for the frontend CSV-reading helpers.

The browser owns CSV parse/decode (see CLAUDE.md), and those functions live
inline in `public/index.html` with no build step. Rather than add a JS toolchain,
this test extracts the pure functions and runs them through `node` directly. It
skips cleanly when node isn't installed, so it never blocks `pytest`.

It guards the parts the Python suite can't reach: that `readCsvText` actually
rejects binaries, falls back to windows-1252, and that emoji / CJK / punctuation
survive readCsvText → parseCSV in the browser primitives (arrayBuffer,
TextDecoder).
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "public" / "index.html"


def _extract(src: str, signature: str) -> str:
    """Pull one JS function out of index.html by brace-matching.

    Smoke-test helper only: assumes the body has no unbalanced braces inside
    string/regex literals (true for the functions extracted here).
    """
    start = src.index(signature)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unterminated function: {signature}")


# Self-asserting JS driver: exits non-zero on any failure, prints "OK" on pass.
_DRIVER = textwrap.dedent(
    r"""
    const QUERY_CANDIDATES = ["query", "restaurant", "restaurant_name", "name"];
    const assert = (cond, msg) => { if (!cond) throw new Error("FAIL: " + msg); };
    const fileOf = (bytes) => ({ arrayBuffer: async () => Uint8Array.from(bytes).buffer });
    const u8 = (str) => Array.from(Buffer.from(str, "utf8"));

    (async () => {
      // --- binary detection ---
      assert(looksBinaryBytes(Uint8Array.from([0x50,0x4b,0x03,0x04])) === true, "PK magic");
      assert(looksBinaryBytes(Uint8Array.from(u8("a,b\x00c"))) === true, "NUL byte");
      assert(looksBinaryBytes(Uint8Array.from(u8("query\nFoo\n"))) === false, "plain CSV");

      // --- emoji / CJK / punctuation round-trip (UTF-8) ---
      const names = ["Burger 🍔 Joint", "Café Crème", "海底捞 Vivocity",
                     "Al-Ameen@Hillview - Bamboo Grove", "#Foodcoholic - 40 Circular Rd"];
      for (const name of names) {
        const text = await readCsvText(fileOf(u8("query\n" + name + "\n")));
        const { headers, dataRows } = parseCSV(text);
        assert(dataRows[0][detectQueryColumn(headers)] === name, "utf8 roundtrip: " + name);
      }

      // --- windows-1252 fallback: é is byte 0xE9 (invalid UTF-8 on its own) ---
      // "query\nCafé\n" in cp1252/latin-1.
      const cp1252 = [0x71,0x75,0x65,0x72,0x79,0x0a,0x43,0x61,0x66,0xE9,0x0a];
      let decoded = null;
      try { decoded = await readCsvText(fileOf(cp1252)); } catch (e) { decoded = null; }
      if (decoded !== null) {  // skip if this node build lacks windows-1252 (small-ICU)
        const { headers, dataRows } = parseCSV(decoded);
        assert(dataRows[0][detectQueryColumn(headers)] === "Café", "cp1252 café");
      }

      // --- binary upload is rejected ---
      let threw = false;
      try { await readCsvText(fileOf([0x50,0x4b,0x03,0x04,0x14,0x00])); }
      catch (e) { threw = true; }
      assert(threw, "binary upload rejected");

      console.log("OK");
    })().catch((e) => { console.error(e.message); process.exit(1); });
    """
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_csv_reading_smoke(tmp_path):
    src = INDEX.read_text(encoding="utf-8")
    funcs = "\n".join(
        _extract(src, sig)
        for sig in (
            "function looksBinaryBytes(",
            "async function readCsvText(",
            "function parseCSV(",
            "function detectQueryColumn(",
        )
    )
    script = tmp_path / "smoke.mjs"
    script.write_text(funcs + "\n" + _DRIVER, encoding="utf-8")

    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "OK" in result.stdout
