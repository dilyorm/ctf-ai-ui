"""Category playbooks — concrete first-moves injected into the solver prompt.

CTF-agent research (EnIGMA's interactive tools, CRAKEN's write-up hint
injection, "ReAct & Plan" baselines) consistently finds that a short,
category-specific opening playbook lifts solve rates: it stops the model
flailing on generic exploration and points it at the tool + technique that
usually cracks that category. These are terse, high-signal starters, not full
walkthroughs; the model still has to think.

``tactics_for(category, tags)`` returns a markdown block, or "" for an unknown
category (so the prompt stays clean).
"""

from __future__ import annotations

# Keyed by a normalized category token. Kept short — each line is a first move.
PLAYBOOKS: dict[str, list[str]] = {
    "web": [
        "Map the app first: `curl -sk` the target, read HTML/JS source, check `/robots.txt`, `/sitemap.xml`, cookies, and response headers.",
        "Fuzz paths/params with `ffuf`/`gobuster` (wordlists in `/usr/share/seclists`). Look for `?debug=`, `/admin`, backup files (`.bak`, `~`, `.git/`).",
        "Test the usual bugs in order: SQLi (`sqlmap -u ... --batch`), SSTI (`{{7*7}}`), path traversal (`../`), IDOR, auth bypass, deserialization.",
        "For SSRF/XSS/blind callbacks use a `webhook.site` URL (or the `webhook_create` tool) and watch for the hit.",
        "JWT: try `alg=none`, weak HMAC secret (`hashcat -m 16500`), `kid` header injection.",
    ],
    "pwn": [
        "`file ./bin` then `checksec --file=./bin` — note arch, RELRO, canary, NX, PIE. This decides the technique.",
        "Find the offset with a cyclic pattern (`pwntools cyclic`), confirm control of RIP in gdb (`pwndbg`).",
        "No canary + NX + no PIE → ret2libc / ROP (`ROPgadget`, `one_gadget`). Leak libc via `puts@plt(puts@got)`, return to main, pop shell.",
        "Format string → `%p` leaks, then targeted `%n` writes with `fmtstr_payload`.",
        "Heap → identify the allocator bug (UAF, tcache dup, overflow) in gdb; `pwndbg`'s `heap`/`bins`. Drive the remote with a pwntools `remote()` script, `stty raw -echo` first.",
    ],
    "rev": [
        "`file` + `strings -n 6` for quick wins (flag, hints, packer). If packed (UPX) `upx -d`.",
        "Decompile: pyghidra (installed) or `r2 -A` then `pdf @ main` / `radare2` visual. For .NET use `ilspycmd`, for Go/Rust rely on strings + structure.",
        "Trace logic dynamically with gdb/`ltrace`/`strace`; for input-checkers, `angr` symbolic execution to solve the constraint that prints 'correct'.",
        "Bytecode (Python `.pyc`) → `decompyle3`/`uncompyle6` or `dis`. Obfuscated JS → beautify + trace.",
    ],
    "crypto": [
        "Identify the primitive first: RSA, AES(mode), XOR, classical, ECC, hashing. Read the source/params carefully.",
        "RSA: check small e (cube root), shared/near primes, Fermat, Wiener (small d), common modulus, LSB oracle. Tools: `RsaCtfTool`, `sage`.",
        "AES: ECB (byte-at-a-time), CBC bit-flipping / padding oracle, nonce reuse (CTR/GCM). Weak PRNG (LCG/Mersenne) → recover state from outputs.",
        "XOR: `xortool` for repeating-key; known-plaintext (flag prefix) recovers the key.",
        "Reach for `sage` for lattices/discrete-log; use `pycryptodome` for glue.",
    ],
    "forensics": [
        "`file` everything; `binwalk -e` for embedded data; `strings`/`grep -a` for the flag format.",
        "Images: `exiftool`, `zsteg` (PNG/BMP LSB), `steghide extract` (JPG, try empty + wordlist passphrase), `stegsolve`-style plane analysis.",
        "PCAP: `tshark`/wireshark — follow TCP/HTTP streams, export objects, look for creds and transferred files.",
        "Memory dump: `volatility3` (`windows.pslist`, `.cmdline`, `.filescan`, `.dumpfiles`). Disk: `mount`/`autopsy`/`testdisk` for deleted files.",
        "Office/PDF macros: `oletools` (`olevba`), `pdf-parser`.",
    ],
    "misc": [
        "Read the prompt literally — misc often hides the technique in the wording. Check attached files with `file`/`strings`/`binwalk`.",
        "Jails/pyjail: bypass filters with `getattr`, `__import__`, `().__class__.__mro__`, builtins tricks. Bash jails: `${IFS}`, brace/hex/`$'\\x..'` encoding.",
        "Encodings: base64/32/85, hex, ROT, morse, brainfuck — `CyberChef`-style transforms via python.",
        "QR/barcodes: `zbarimg`. Audio: spectrogram (`sox`/Audacity) for hidden text/DTMF.",
    ],
    "osint": [
        "Pivot on every handle/name/email; reverse-image search; read EXIF GPS from photos (`exiftool`).",
        "Check archive.org/wayback, git history, pastebin, and public social profiles for leaked flag pieces.",
    ],
    "blockchain": [
        "Read the contract source/bytecode; for Solidity check reentrancy, unchecked call, `tx.origin`, integer issues, unprotected `selfdestruct`/`delegatecall`.",
        "Interact with `cast`/`web3.py` against the given RPC; decompile bytecode with `panoramix`/`heimdall` if no source.",
    ],
}

# Category aliases → canonical playbook key.
_ALIASES = {
    "web": "web", "webexploitation": "web", "web-exploitation": "web",
    "pwn": "pwn", "binary": "pwn", "binaryexploitation": "pwn", "binary-exploitation": "pwn", "exploitation": "pwn",
    "rev": "rev", "reversing": "rev", "reverse": "rev", "reverse-engineering": "rev", "re": "rev",
    "crypto": "crypto", "cryptography": "crypto",
    "forensics": "forensics", "forensic": "forensics", "dfir": "forensics",
    "misc": "misc", "miscellaneous": "misc", "jail": "misc", "programming": "misc",
    "osint": "osint", "recon": "osint",
    "stego": "forensics", "steganography": "forensics",
    "blockchain": "blockchain", "smart-contract": "blockchain", "web3": "blockchain",
}


def _canonical(category: str) -> str | None:
    key = (category or "").strip().lower().replace(" ", "").replace("_", "-")
    if key in _ALIASES:
        return _ALIASES[key]
    # loose contains-match (e.g. "pwn/heap")
    for token, canon in _ALIASES.items():
        if token in key:
            return canon
    return None


def tactics_for(category: str, tags: list[str] | None = None) -> str:
    """Return a markdown '## Category playbook' block, or '' if unknown."""
    canon = _canonical(category)
    if not canon and tags:
        for t in tags:
            canon = _canonical(str(t))
            if canon:
                break
    if not canon:
        return ""
    lines = PLAYBOOKS.get(canon, [])
    if not lines:
        return ""
    out = [f"## {canon.capitalize()} playbook (first moves)"]
    out += [f"{i}. {line}" for i, line in enumerate(lines, 1)]
    out.append("")
    return "\n".join(out)
