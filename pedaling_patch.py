#!/usr/bin/env python3
"""
pedaling_patch.py — Section 11 sync.py patch v3.113
====================================================
Agrega extracción de métricas de pedaleo desde el .fit original de COROS:
  - Balance L/R  (stance_time_balance → campo propietario COROS)
  - Distribución de cadencia (5 bins)
  - Coasting % (tiempo con potencia = 0)
  - Correlación potencia-cadencia (Pearson r)
  - HR drop final 60s (aproximación HRRc para sesiones no estructuradas)
  - Laps del dispositivo (complemento de icu_intervals)

Laps incluidos → no es necesario hacer setup manual de intervalos en
Intervals.icu para que Claude Code vea la estructura básica de la sesión.
Los icu_intervals siguen siendo la fuente para zone/decoupling/training_load.

Uso (desde la raíz del repo, con Claude Code):
    python3 pedaling_patch.py          # dry-run, muestra qué cambia
    python3 pedaling_patch.py --apply  # aplica el parche a sync.py
    python3 pedaling_patch.py --check  # verifica si el parche ya fue aplicado
"""

import sys
import re
import shutil
from pathlib import Path
from datetime import datetime

SYNC_PATH = Path("sync.py")

# ── Dependencia requerida ──────────────────────────────────────────────────────
FITDECODE_IMPORT_ANCHOR = "import requests"
FITDECODE_CHECK = "# fitdecode: required for .fit pedaling extraction"

# ── Nuevo método ───────────────────────────────────────────────────────────────
# Se inserta justo antes de _fetch_activity_intervals
METHOD_ANCHOR = "    def _fetch_activity_intervals(self, activity_id: str) -> tuple:"

NEW_METHOD = '''    def _fetch_pedaling_from_fit(self, activity_id: str) -> tuple:
        """
        Download the original .fit file from Intervals.icu and extract
        pedaling metrics not available via the standard streams API.

        COROS devices write L/R balance to `stance_time_balance` (a FIT
        running field) instead of the standard cycling `left_right_balance`.
        Intervals.icu does not map this field — it never surfaces in the UI
        or streams API. This fetcher reads the raw .fit to recover it.

        Also extracts: cadence distribution, coasting %, power-cadence
        correlation, HR drop (end-of-session proxy for HRRc), and COROS
        lap splits.

        Returns (status, payload):
            ("ok", {"pedaling": {...}, "laps": [...]})
            ("no_data", {})             — .fit has no usable pedaling data
            ("terminal_error", "http_NNN") — 404/410
            ("transient", reason)       — network/timeout/parse errors;
                                          entry saved without pedaling block,
                                          flagged pedaling_retry=true for
                                          re-attempt on next sync

        v3.113 — COROS stance_time_balance mapping
        """
        import gzip
        import io
        import statistics as _stats

        url = f"{self.INTERVALS_BASE_URL}/activity/{activity_id}/file"
        headers = {"Authorization": f"Basic {self.intervals_auth}"}

        try:
            response = requests.get(url, headers=headers, timeout=60)
        except requests.exceptions.Timeout:
            return ("transient", "timeout")
        except requests.exceptions.RequestException as e:
            return ("transient", f"network: {str(e)[:80]}")

        if response.status_code == 200:
            pass
        elif response.status_code in (404, 410):
            return ("terminal_error", f"http_{response.status_code}")
        else:
            return ("transient", f"http_{response.status_code}")

        # Decompress gzip
        try:
            raw = gzip.decompress(response.content)
            fit_io = io.BytesIO(raw)
        except Exception as e:
            return ("transient", f"decompress_error: {str(e)[:60]}")

        # Parse with fitdecode (pip install fitdecode)
        try:
            import fitdecode
        except ImportError:
            return ("transient", "fitdecode_not_installed — run: pip install fitdecode")

        records = []
        laps_raw = []

        try:
            with fitdecode.FitReader(fit_io) as fit:
                for frame in fit:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    row = {f.name: f.value for f in frame.fields if f.value is not None}
                    if frame.name == "record":
                        records.append(row)
                    elif frame.name == "lap":
                        laps_raw.append(row)
        except Exception as e:
            return ("transient", f"parse_error: {str(e)[:80]}")

        if not records:
            return ("no_data", {})

        # ── L/R Balance (stance_time_balance — COROS proprietary mapping) ──────
        # COROS stores L/R power balance in this FIT running field instead of
        # the standard left_right_balance. Values 30-70 are valid balance %;
        # 0 and 255 are no-data sentinels.
        balance_vals = [
            r["stance_time_balance"] for r in records
            if "stance_time_balance" in r and 30 <= r["stance_time_balance"] <= 70
        ]
        balance_block = None
        if len(balance_vals) >= 60:
            avg_left = round(_stats.mean(balance_vals), 1)
            balance_block = {
                "avg_left_pct": avg_left,
                "avg_right_pct": round(100.0 - avg_left, 1),
                "cv_pct": round(
                    (_stats.stdev(balance_vals) / avg_left * 100)
                    if avg_left > 0 and len(balance_vals) > 1 else 0, 1
                ),
                "coverage_pct": round(len(balance_vals) / len(records) * 100, 1),
                "source": "stance_time_balance_coros"
            }

        # ── Cadence distribution ─────────────────────────────────────────────
        cadences = [r["cadence"] for r in records if "cadence" in r and r["cadence"] > 0]
        cadence_block = None
        if len(cadences) >= 60:
            total = len(cadences)
            cadence_block = {
                "avg_rpm": round(_stats.mean(cadences), 1),
                "median_rpm": int(_stats.median(cadences)),
                "sub70_pct":   round(sum(1 for c in cadences if c < 70)           / total * 100, 1),
                "r70_80_pct":  round(sum(1 for c in cadences if 70 <= c < 80)     / total * 100, 1),
                "r80_90_pct":  round(sum(1 for c in cadences if 80 <= c < 90)     / total * 100, 1),
                "r90_100_pct": round(sum(1 for c in cadences if 90 <= c < 100)    / total * 100, 1),
                "over100_pct": round(sum(1 for c in cadences if c >= 100)         / total * 100, 1),
            }

        # ── Coasting (zero power) ────────────────────────────────────────────
        powers = [r["power"] for r in records if "power" in r]
        coasting_pct = None
        if len(powers) >= 60:
            coasting_pct = round(sum(1 for p in powers if p == 0) / len(powers) * 100, 1)

        # ── Power-Cadence correlation (Pearson r) ────────────────────────────
        power_cadence_corr = None
        paired = [
            (r["power"], r["cadence"]) for r in records
            if "power" in r and "cadence" in r
            and r["power"] > 50 and r["cadence"] > 60
        ]
        if len(paired) >= 60:
            try:
                w_list, c_list = zip(*paired)
                mean_w = _stats.mean(w_list)
                mean_c = _stats.mean(c_list)
                n = len(paired)
                cov = sum((w - mean_w) * (c - mean_c) for w, c in paired) / n
                std_w = _stats.stdev(w_list)
                std_c = _stats.stdev(c_list)
                if std_w > 0 and std_c > 0:
                    power_cadence_corr = round(cov / (std_w * std_c), 3)
            except Exception:
                pass

        # ── HR drop final 60s (end-of-session HRRc proxy) ───────────────────
        # NOTE: not the same as the Section 11 HRRc (which requires a structured
        # hard effort + recovery window). This is the drop from -120s to -60s
        # from end of session — useful for unstructured rides where true HRRc
        # cannot be computed. Requires ≥180 HR samples (3 min with HR data).
        hr_series = [r["heart_rate"] for r in records if "heart_rate" in r]
        hr_drop_final_60s = None
        if len(hr_series) >= 180:
            try:
                seg_minus2 = hr_series[-120:-60]
                seg_last   = hr_series[-60:]
                hr_drop_final_60s = round(
                    _stats.mean(seg_minus2) - _stats.mean(seg_last)
                )
            except Exception:
                pass

        # ── Laps from device ─────────────────────────────────────────────────
        # COROS lap messages = manual lap presses or auto-lap triggers.
        # Complement (not replacement) for icu_intervals: no zone/decoupling/
        # training_load, but available immediately without manual Intervals setup.
        laps_out = []
        for i, lap in enumerate(laps_raw):
            dur = lap.get("total_timer_time")
            if dur is None or dur < 10:
                continue
            laps_out.append({
                "lap":              i + 1,
                "duration_secs":    round(dur),
                "distance_km":      round(lap["total_distance"] / 1000, 2)
                                    if "total_distance" in lap else None,
                "avg_power":        lap.get("avg_power"),
                "normalized_power": lap.get("normalized_power"),
                "avg_hr":           lap.get("avg_heart_rate"),
                "min_hr":           lap.get("min_heart_rate"),
                "max_hr":           lap.get("max_heart_rate"),
                "avg_cadence":      round(lap["avg_cadence"])
                                    if "avg_cadence" in lap else None,
                "total_ascent_m":   lap.get("total_ascent"),
                "total_descent_m":  lap.get("total_descent"),
                "calories":         lap.get("total_calories"),
            })

        # ── Assemble output ──────────────────────────────────────────────────
        pedaling = {}
        if balance_block:
            pedaling["balance"] = balance_block
        if cadence_block:
            pedaling["cadence_distribution"] = cadence_block
        if coasting_pct is not None:
            pedaling["coasting_pct"] = coasting_pct
        if power_cadence_corr is not None:
            pedaling["power_cadence_corr"] = power_cadence_corr
        if hr_drop_final_60s is not None:
            pedaling["hr_drop_final_60s"] = hr_drop_final_60s

        if not pedaling and not laps_out:
            return ("no_data", {})

        return ("ok", {
            "pedaling": pedaling if pedaling else None,
            "laps":     laps_out  if laps_out  else None,
        })

'''

# ── Integration point ──────────────────────────────────────────────────────────
# Inject pedaling/laps fetch right before new_entries.append(entry)
ENTRY_ANCHOR = '''                if dfa_block is not None:
                    entry["dfa"] = dfa_block
                new_entries.append(entry)'''

ENTRY_REPLACEMENT = '''                if dfa_block is not None:
                    entry["dfa"] = dfa_block

                # v3.113: fetch pedaling metrics + laps from original .fit
                # On transient failure: entry saved without pedaling block,
                # pedaling_retry=true signals re-attempt on next sync cycle.
                pedaling_status, pedaling_payload = self._fetch_pedaling_from_fit(act_id)
                if pedaling_status == "ok":
                    if pedaling_payload.get("pedaling"):
                        entry["pedaling"] = pedaling_payload["pedaling"]
                    if pedaling_payload.get("laps"):
                        entry["laps"] = pedaling_payload["laps"]
                elif pedaling_status == "transient":
                    entry["pedaling_retry"] = True
                    if self.debug:
                        print(f"    ⚠️  FIT pedaling transient for {act_id}: {pedaling_payload} (retry next sync)")
                # terminal_error / no_data: omit block silently (no retry needed)

                new_entries.append(entry)'''

# ── Retry logic: re-fetch pedaling for cached entries flagged pedaling_retry ──
# Injected into the candidates loop, after cached_ids is populated
RETRY_ANCHOR = '''        candidates = []
        for act in activities:
            date_str = act.get("start_date_local", "")[:10]
            if date_str < scan_cutoff:
                continue
            act_type = act.get("type", "")
            family = self.SPORT_FAMILIES.get(act_type)
            if family not in self.INTERVAL_SPORT_FAMILIES:
                continue
            act_id = act.get("id")
            if act_id in cached_ids:
                continue
            candidates.append(act)'''

RETRY_REPLACEMENT = '''        # v3.113: retry pedaling fetch for cached entries that previously failed
        retry_pedaling_ids = {
            a["activity_id"] for a in cached.get("activities", [])
            if a.get("pedaling_retry") is True
        }
        if retry_pedaling_ids and self.debug:
            print(f"    🔄 Retrying pedaling fetch for {len(retry_pedaling_ids)} cached activit{'y' if len(retry_pedaling_ids)==1 else 'ies'}...")
        for cached_entry in cached.get("activities", []):
            cid = cached_entry.get("activity_id")
            if cid not in retry_pedaling_ids:
                continue
            p_status, p_payload = self._fetch_pedaling_from_fit(cid)
            if p_status == "ok":
                if p_payload.get("pedaling"):
                    cached_entry["pedaling"] = p_payload["pedaling"]
                if p_payload.get("laps"):
                    cached_entry["laps"] = p_payload["laps"]
                cached_entry.pop("pedaling_retry", None)
                if self.debug:
                    print(f"    ✅ Pedaling retry ok: {cid}")
            elif p_status != "transient":
                # terminal_error or no_data — stop retrying
                cached_entry.pop("pedaling_retry", None)

        candidates = []
        for act in activities:
            date_str = act.get("start_date_local", "")[:10]
            if date_str < scan_cutoff:
                continue
            act_type = act.get("type", "")
            family = self.SPORT_FAMILIES.get(act_type)
            if family not in self.INTERVAL_SPORT_FAMILIES:
                continue
            act_id = act.get("id")
            if act_id in cached_ids:
                continue
            candidates.append(act)'''


# ── Patch engine ──────────────────────────────────────────────────────────────

def check_applied(content: str) -> dict:
    return {
        "method":     "_fetch_pedaling_from_fit" in content,
        "entry_hook": "pedaling_retry" in content,
        "retry_loop": "retry_pedaling_ids" in content,
    }

def apply_patch(content: str) -> str:
    status = check_applied(content)

    # 1. Insert new method before _fetch_activity_intervals
    if not status["method"]:
        if METHOD_ANCHOR not in content:
            raise ValueError(f"Anchor not found: {METHOD_ANCHOR!r}")
        content = content.replace(METHOD_ANCHOR, NEW_METHOD + METHOD_ANCHOR, 1)
        print("  ✅ Inserted _fetch_pedaling_from_fit method")
    else:
        print("  ⏭  Method already present — skipped")

    # 2. Inject call in entry builder
    if not status["entry_hook"]:
        if ENTRY_ANCHOR not in content:
            raise ValueError(f"Entry anchor not found. Ensure sync.py matches expected structure.")
        content = content.replace(ENTRY_ANCHOR, ENTRY_REPLACEMENT, 1)
        print("  ✅ Injected pedaling/laps call in entry builder")
    else:
        print("  ⏭  Entry hook already present — skipped")

    # 3. Inject retry loop
    if not status["retry_loop"]:
        if RETRY_ANCHOR not in content:
            raise ValueError(f"Retry anchor not found. Ensure sync.py matches expected structure.")
        content = content.replace(RETRY_ANCHOR, RETRY_REPLACEMENT, 1)
        print("  ✅ Injected pedaling_retry loop in candidates builder")
    else:
        print("  ⏭  Retry loop already present — skipped")

    return content


def main():
    dry_run = "--apply" not in sys.argv
    check_mode = "--check" in sys.argv

    if not SYNC_PATH.exists():
        print(f"❌  {SYNC_PATH} not found. Run this script from the root of your training-data repo.")
        sys.exit(1)

    content = SYNC_PATH.read_text(encoding="utf-8")
    status = check_applied(content)

    if check_mode:
        print("=== Patch status ===")
        all_ok = True
        for key, applied in status.items():
            icon = "✅" if applied else "❌"
            print(f"  {icon} {key}")
            if not applied:
                all_ok = False
        sys.exit(0 if all_ok else 1)

    if all(status.values()):
        print("✅  Patch already fully applied. Nothing to do.")
        sys.exit(0)

    if dry_run:
        print("=== DRY RUN (use --apply to write changes) ===")
        print("Pending changes:")
        if not status["method"]:
            print("  • Insert _fetch_pedaling_from_fit() method")
        if not status["entry_hook"]:
            print("  • Inject pedaling/laps call in entry builder")
        if not status["retry_loop"]:
            print("  • Inject pedaling_retry loop in candidates builder")
        print("\nRun with --apply to apply.")
        sys.exit(0)

    # Backup
    backup = SYNC_PATH.with_suffix(f".py.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(SYNC_PATH, backup)
    print(f"📦  Backup: {backup}")

    try:
        new_content = apply_patch(content)
    except ValueError as e:
        print(f"❌  Patch failed: {e}")
        print("    Your sync.py may differ from the expected version.")
        print("    Share the error with your coach for a manual patch.")
        sys.exit(1)

    SYNC_PATH.write_text(new_content, encoding="utf-8")
    print(f"\n✅  sync.py patched successfully (v3.113)")
    print("\nPróximos pasos:")
    print("  1. pip install fitdecode  (si no está instalado)")
    print("  2. rm intervals.json      (para que sync rehaga todos los entries)")
    print("  3. python sync.py --output latest.json")
    print("\nEn intervals.json cada actividad ciclista tendrá:")
    print("  • pedaling.balance         — L/R % (Favero Assioma vía COROS)")
    print("  • pedaling.cadence_distribution — distribución en 5 bins")
    print("  • pedaling.coasting_pct    — % tiempo sin pedalear")
    print("  • pedaling.power_cadence_corr — Pearson r")
    print("  • pedaling.hr_drop_final_60s  — proxy HRRc fin de sesión")
    print("  • laps[]                   — splits del dispositivo")


if __name__ == "__main__":
    main()
