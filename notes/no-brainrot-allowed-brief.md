## Prior progress (do NOT redo this work)

Two prior attempts (Claude Opus 4.7, GPT-5.3 Codex) failed with `gave_up`. **No flag obtained.**

### Server / oracle (verified by both runs)

`server.py` is a textbook RSA-1024 decryption oracle:

- `n` = 1024-bit modulus, hardcoded. `e = 65537`.
- Banner prints `Your flag: <flag_ct>` then `Your messages: ` prompt.
- Input: comma-separated decimal ciphertexts on one line.
- For each ct: server computes `pt = pow(ct, d, n)`, then checks if the string `"67"` appears anywhere in `hex(pt)` (the `"0x..."` representation).
- Two response strings:
  - `"BRAINROT DETECTED"` if `"67"` appears in `hex(pt)`
  - `"... thanks you ..."` otherwise.

Hint *"n has been specifically selected to not contain the forbidden two digit sequence"* means `hex(n)` itself has no `"67"`, so the oracle is purely a function of `pt`.

### Vulnerability — confirmed

**Bleichenbacher-style MSB oracle.** A query `ct·s^e mod n` reveals whether `m·s mod n` lies in
`J = ⋃_{k=2..256} [0x67·16^(k−2), 0x68·16^(k−2))`
≈ 1/16 hit rate per query, dominated by `k=254` slice (width ≈ n/16).
Classic BB98 / Manger-style interval narrowing recovers `m`.

### What partially worked

- **Codex** built clean BB iteration code, verified `trial ok` ×5 against an offline simulator. Got blinding factor `s_0 = 310` after **384 queries** on the live server. Crashed in step 2a (`empty interval at i=2, IndexError`) because vanilla BB98 assumes a single fixed interval — but this oracle's accept set is a **union over k**.
- **Opus** collected **285 verified `j=254` pairs** at `/challenge/workspace/verified_j254.txt` (only valid for that session's `c_flag`; reverify after reconnect). Then burned ~1100 steps on lattice/HNP variants (lengths 36, 38, 40, 42, 84, 111) — none recovered `m`.
- Wiener and `RsaCtfTool --attack all` ruled out — n does not factor.

### What to do next (concrete)

1. Reconnect, parse banner `c_flag = pow(m, 65537, n) mod n`.
2. **Use the union-of-intervals BB algorithm.** Maintain `M = list[(a,b)]` of candidate ranges for `m·s_i`. For each `(L,U) ∈ J = [(0x67·16^(t−2), 0x68·16^(t−2)−1) for t in 2..256]`, intersect with `M` to derive `r ∈ [⌈(a·s − U)/n⌉, ⌊(b·s − L)/n⌋]`. **This is the codex code already at trace lines 50/53/56 — only step-2 (interval propagation) needs the union fix.**
3. Re-derive `s_0` (≈300–500 queries with batch=128) — DO NOT reuse the old `s_0=310` since `c_flag` changes per connect.
4. Step `s_i` upward sequentially with batch=128 oracle calls. Total ≈ a few thousand queries; one connection sustains ≥384.
5. Stop when M collapses to one interval `(a,b)` with `b−a < 2^(8·flag_len)`. Decode `long_to_bytes(a)` and look for `UMDCTF{...}`. Do NOT bake flag length in.

**Avoid:** Wiener, RsaCtfTool, lattice/HNP — all already exhausted.

Files of interest in trace: `trace-no-brainrot-allowed-gpt-5.3-codex-20260425-121025.jsonl` lines 44/47/50/53/56 (working BB code) — same trace persisted in `/root/ctf-ai-ui/logs/`.
