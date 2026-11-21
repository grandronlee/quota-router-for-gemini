from quota_router.classify import Classification
from quota_router.clock import next_pt_midnight
from quota_router.quota import Blocked, Reservation

from helpers import T0, Env, make_config, model_dict, pt


def env_with(**limits) -> Env:
    return Env(make_config({"a": model_dict("model-a", **limits)}))


def test_tpm_counts_input_only_and_skips_when_free_space_is_too_small():
    e = env_with(tpm_input=1000)
    a = e.model("a")
    assert isinstance(e.store.try_reserve(a, 600), Reservation)
    b = e.store.try_reserve(a, 500)  # 400 free < 500
    assert isinstance(b, Blocked) and b.reason == "quota" and "tpm" in b.detail
    assert b.ready_at == T0 + 60
    assert isinstance(e.store.try_reserve(a, 400), Reservation)  # exactly fills the window
    e.clock.advance(60)
    assert isinstance(e.store.try_reserve(a, 1000), Reservation)


def test_reported_prompt_tokens_replace_the_estimate():
    e = env_with(tpm_input=1000)
    a = e.model("a")
    res = e.store.try_reserve(a, 900)
    assert isinstance(e.store.try_reserve(a, 800), Blocked)
    e.store.settle(res, 100)
    assert isinstance(e.store.try_reserve(a, 800), Reservation)
    assert e.store.snapshot()["model-a"]["tpm_input_used"] == 900


def test_settle_without_usage_keeps_the_estimate_and_runs_once():
    e = env_with(tpm_input=1000)
    a = e.model("a")
    res = e.store.try_reserve(a, 900)
    e.store.settle(res, None)
    e.store.settle(res, 10)  # second settle is ignored
    assert e.store.snapshot()["model-a"]["tpm_input_used"] == 900


def test_rpm_window_rolls():
    e = env_with(rpm=2)
    a = e.model("a")
    e.store.try_reserve(a, 1)
    e.clock.advance(10)
    e.store.try_reserve(a, 1)
    b = e.store.try_reserve(a, 1)
    assert isinstance(b, Blocked) and b.detail == "rpm" and b.ready_at == T0 + 60
    e.clock.set(T0 + 60)
    assert isinstance(e.store.try_reserve(a, 1), Reservation)


def test_rpd_limit_blocks_until_pt_midnight_then_resets():
    e = env_with(rpd=2, rpm=100)
    a = e.model("a")
    e.store.try_reserve(a, 1)
    e.store.try_reserve(a, 1)
    b = e.store.try_reserve(a, 1)
    assert isinstance(b, Blocked) and b.detail == "rpd" and b.ready_at == next_pt_midnight(T0)
    e.clock.set(next_pt_midnight(T0))
    assert isinstance(e.store.try_reserve(a, 1), Reservation)
    assert e.store.snapshot()["model-a"]["rpd_used"] == 1


def test_refund_rpd_is_idempotent_and_ignores_a_past_day():
    e = env_with(rpd=5)
    a = e.model("a")
    r1 = e.store.try_reserve(a, 1)
    e.store.try_reserve(a, 1)
    e.store.refund_rpd(r1)
    e.store.refund_rpd(r1)
    assert e.store.snapshot()["model-a"]["rpd_used"] == 1
    r3 = e.store.try_reserve(a, 1)
    e.clock.set(next_pt_midnight(T0) + 5)
    e.store.try_reserve(a, 1)
    e.store.refund_rpd(r3)  # yesterday's slot: today's count is untouched
    assert e.store.snapshot()["model-a"]["rpd_used"] == 1


def test_per_day_429_exhausts_until_pt_midnight():
    e = env_with()
    a = e.model("a")
    e.store.on_rate_limited(a, Classification("rpd", 30.0))
    b = e.store.check(a, 1)
    assert b.reason == "quota" and b.ready_at == next_pt_midnight(T0)
    e.clock.set(next_pt_midnight(T0))
    assert e.store.check(a, 1) is None


def test_per_minute_and_token_429_cooldowns():
    e = env_with()
    a = e.model("a")
    e.store.on_rate_limited(a, Classification("rpm", 20.0))
    assert e.store.check(a, 1).ready_at == T0 + 20
    e.clock.advance(20)
    assert e.store.check(a, 1) is None

    e.store.on_rate_limited(a, Classification("tpm", 5.0))
    b = e.store.check(a, 1)
    assert b.ready_at == e.clock.now() + 60  # TPM window treated as full for a minute
    assert "tpm" in b.detail


def test_unknown_429_cools_down_at_least_60s():
    e = env_with()
    a = e.model("a")
    e.store.on_rate_limited(a, Classification("unknown", None))
    assert e.store.check(a, 1).ready_at == T0 + 60
    e.store.on_rate_limited(a, Classification("unknown", 5.0))
    assert e.store.check(a, 1).ready_at == T0 + 60


def test_5xx_cooldown_is_unavailable_unless_quota_also_blocks():
    e = env_with(rpd=1)
    a = e.model("a")
    e.store.on_unavailable(a)
    b = e.store.check(a, 1)
    assert (b.reason, b.ready_at) == ("unavailable", T0 + 30)
    e.clock.advance(30)
    e.store.try_reserve(a, 1)  # uses the only RPD slot
    e.store.on_unavailable(a)
    b = e.store.check(a, 1)
    assert b.reason == "quota" and b.ready_at == next_pt_midnight(T0)


def test_rpd_and_exhausted_survive_a_restart_and_reset_on_the_next_pt_date(tmp_path):
    db = str(tmp_path / "q.sqlite3")
    cfg = make_config({"a": model_dict("model-a", rpd=3), "b": model_dict("model-b")})
    e1 = Env(cfg, db=db)
    e1.store.try_reserve(e1.model("a"), 1)
    e1.store.try_reserve(e1.model("a"), 1)
    e1.store.on_rate_limited(e1.model("b"), Classification("rpd", None))
    e1.conn.close()

    e2 = Env(cfg, db=db, start=T0 + 3600)  # restart later the same day
    snap = e2.store.snapshot()
    assert snap["model-a"]["rpd_used"] == 2
    assert snap["model-b"]["exhausted_until"] == next_pt_midnight(T0)
    e2.store.try_reserve(e2.model("a"), 1)
    assert isinstance(e2.store.try_reserve(e2.model("a"), 1), Blocked)
    e2.conn.close()

    e3 = Env(cfg, db=db, start=pt(2026, 11, 21, 0, 0, 1))  # restart on the next PT date
    snap = e3.store.snapshot()
    assert snap["model-a"]["rpd_used"] == 0
    assert snap["model-b"]["exhausted_until"] is None
    assert e3.store.check(e3.model("b"), 1) is None


def test_old_rpd_rows_are_pruned(tmp_path):
    db = str(tmp_path / "q.sqlite3")
    cfg = make_config({"a": model_dict("model-a")})
    e1 = Env(cfg, db=db)
    e1.store.try_reserve(e1.model("a"), 1)
    e1.conn.close()
    e2 = Env(cfg, db=db, start=T0 + 10 * 86400)
    assert e2.conn.execute("SELECT COUNT(*) FROM rpd").fetchone()[0] == 0
