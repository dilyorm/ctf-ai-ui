## Prior progress (do NOT redo this work)

Prior Opus 4.7 + Codex attempts: ~$348 burned, 12 wake-up cycles, **no flag**. Most time wasted reading Mesa source.

### Architecture (verified)

Files at `/root/ctf-ai-ui/challenges/vkexchange/distfiles/{vkexchange.c,settlement.comp,Dockerfile,Makefile}`.

**Host (`vkexchange.c`, ~899 lines)** — Vulkan compute on `llvmpipe`/`lavapipe`:
- `accounts[8]`: host-visible `VkBuffer`, size 4..0x1000, mapped pointer in `App.accounts[i].map`. User read/write via menu 2/3.
- `oracle_buf` (256 B = 64 u32): filled with `getenv("FLAG")` in `init_resolution()` (line 601). **The flag literally lives in this storage buffer.**
- `clearing_buf` (256 B): output that the shader writes into. **No menu reads it directly — readback is the second blocker.**
- `quote_book` desc set: binding 0 = STORAGE_BUFFER (`descriptorCount=1`) + binding 1 = INLINE_UNIFORM_BLOCK (8 B).
- `settlement_book` desc set: 2 SSBOs (oracle readonly @ binding 0, clearing writeonly @ binding 1).
- Per-market layouts use `descriptorCount = m->storage_count` (user-supplied, ≤32768) for binding 0 — allocated but **not used by any pipeline**.

**Shader (`settlement.comp`):** Trivial. Push constant `{mode,index,value}`. With `index<64`:
- mode 0: `clearing[index] = oracle[index]`
- mode 1: `clearing[0] = oracle[index]`
- mode 2: `clearing[index] = value`

But `settle_round()` (line 808) hardcodes `mode=0, value=0` — **only `index=word` is user-controlled** via menu 7.

### Protocol (port 30305)

Plain stdin menu: `1`=open_account(bytes), `2`=fund(account,offset,hex), `3`=audit(account,offset,bytes; ≤0x200), `4`=list_market(slots∈[1,32768], memo∈[0,256] mod 4), `5`=open_exchange (requires `outcome_slot_total≥32768`), `6`=quote(price_index∈[32768,300000], account, offset, range≥4), `7`=settle(round<resolution_words≤64), `0`=quit. `armed`/`quoted` flags lock most ops after open/quote.

### Vulnerability — most credible (not yet weaponized)

`menu_quote_position` line 791:
```c
update_storage_desc(app, app->quote_book, 0, (uint32_t)idx, b->buf, off, range);
```
writes the quote SSBO at `dstArrayElement = idx ∈ [32768, 300000]`, but `quote_book` binding 0 has `descriptorCount=1`. **OOB descriptor write into the lavapipe descriptor pool's `set->descriptors[]` array** (`struct lp_descriptor` ≈32 B per slot, see `lvp_descriptor_set.c:448`: `desc += bind_layout->descriptor_index + write->dstArrayElement;`). Validation layers off, `robustBufferAccess=VK_FALSE` — adjacent allocations get trampled.

Pool allocation order (line 593): `quote_book → markets[0..n−1] → settlement_book`. Per-market backing is ~1.1 MB (32768 descriptors × 32 B + headers).

### What was tried (so you don't redo)

- Built locally with `mesa-vulkan-drivers 23.2.1`. Confirmed local `FLAG=...` env-var lands in `oracle_buf`. Mesa 22/23 sources extracted at `/tmp/mesa-mesa-23.2.1/` and `/tmp/mesa-mesa-22.3.6/`.
- LD_PRELOAD'd `posix_memalign` to map heap layout: pool slots are 4096-aligned 256B blocks, market backing 1146880 B blocks.
- Sequence list_market → open_account → fund(`AAAA…`) → arm → quote(`idx=32768`) → settle 0,1,2 → audit produced **no leak, no crash**. The OOB write `idx=32768` was accepted silently.
- pwntools wrapper at `/tmp/explo.py` — never produced a leak.

### Concrete next steps

1. **Don't read Mesa source for >5 min — sweep instead.** Run a local instrumented build that prints `set + descriptor_index + dstArrayElement` from `lvp_UpdateDescriptorSets`. Confirm the address that lands on `settlement_book->descriptors[1]` (the clearing SSBO).
2. **Aliasing strategy for readback** — only viable channel:
   - Goal: rewrite `settlement_book->descriptors[1]` so `clearing` aliases an account buffer's `b->map`. After `settle(round=0..63)` with `mode=0`, `clearing[i] = oracle[i]` writes flag bytes into the account → audit dumps them.
   - Crafted `lp_descriptor` value: `.buffer.u = account->map`, `.buffer.num_elements = size`. Build it inside another account buffer, then trigger `menu_quote_position` with `b = that account` and `idx` chosen so the descriptor write lands on `settlement_book[1]`.
3. **Sweep `idx` in steps of 32** with marker payloads. Map: which `idx` values change which audited output / cause crashes. Start range: just past the one market set, i.e. with **0 extra markets**, idx ≈ 32768 + N where N is small. The agent never did this sweep — start there.
4. **Alternative if aliasing fails:** smash an `lp_descriptor.functions` field for arbitrary call (vtbl-style hijack) — but only if step 3 reveals a crash on a `functions` offset.
5. Use `VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json` and `LIBGL_ALWAYS_SOFTWARE=1`. Built binary at `/challenge/workspace/vkexchange`.

**Avoid:** general Mesa code-reading without a target offset, and `audit_account` len bound exploration (`0x200` cap is fine — accounts are ≤0x1000 but no overflow primitive there).
