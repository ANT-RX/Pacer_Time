import random

from pacer_time.engine import compute
from pacer_time.models import Override, RaceConfig, ReadRec, RunnerInfo, TimingPoint

GUN = 1_000_000_000
POINTS = [
    TimingPoint(1, 1, "Salida", "start", 0, 0),
    TimingPoint(2, 1, "Parcial 1", "split", 1, 2500),
    TimingPoint(3, 1, "Meta", "finish", 2, 5000),
]


def cfg(**kw):
    base = dict(gun_time_ms=GUN, award_mode="hybrid", distance_m=5000, dedupe_window_ms=5000, min_segment_ms=30000, status="running")
    base.update(kw)
    return RaceConfig(**base)


def runner(i, gender="M", cat="Cat", status="REG", chips=None, kit=True):
    return RunnerInfo(i, i, f"Corredor {i}", gender, 1, cat, status, tuple(chips if chips is not None else [f"C{i}"]), kit)


class Reads:
    def __init__(self):
        self.items = []
        self._id = 0

    def add(self, chip, point, ts):
        self._id += 1
        self.items.append(ReadRec(self._id, point, chip, ts))
        return self._id


def test_basic_gun_net_and_pace():
    r = Reads()
    for dt in (2000, 2300, 2900):  # lecturas repetidas en la salida
        r.add("C1", 1, GUN + dt)
    r.add("C1", 2, GUN + 600_000)
    r.add("C1", 3, GUN + 1_200_000)
    r.add("C1", 3, GUN + 1_200_400)
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    row = comp.results[0]
    assert row.status == "FIN"
    assert row.gun_ms == 1_200_000
    assert row.net_ms == 1_198_000
    assert row.pace_ms_km == 1_198_000 * 1000 / 5000
    assert row.splits_ms[2] == 598_000  # desde la salida neta
    assert row.position == row.gender_pos == row.category_pos == 1
    assert comp.duplicate_reads == 3  # 2 en salida + 1 en meta
    assert not [i for i in comp.issues if i.severity == "error"]


def test_start_before_gun_is_clamped_to_gun():
    r = Reads()
    r.add("C1", 1, GUN - 60_000)  # calentamiento sobre el tapete
    r.add("C1", 2, GUN + 500_000)
    r.add("C1", 3, GUN + 1_000_000)
    row = compute(cfg(), POINTS, [runner(1)], r.items).results[0]
    assert row.gun_ms == row.net_ms == 1_000_000


def test_missing_start_falls_back_to_gun_with_warning():
    r = Reads()
    r.add("C1", 2, GUN + 500_000)
    r.add("C1", 3, GUN + 1_000_000)
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    row = comp.results[0]
    assert row.net_ms == row.gun_ms == 1_000_000
    assert "net_fallback" in row.flags
    assert any(i.code == "no_start" for i in comp.issues)


def test_missing_split_warns_but_finishes():
    r = Reads()
    r.add("C1", 1, GUN + 1000)
    r.add("C1", 3, GUN + 1_000_000)
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    assert comp.results[0].status == "FIN"
    assert [i.code for i in comp.issues] == ["no_split"]


def test_reads_before_gun_are_discarded_except_start():
    r = Reads()
    r.add("C1", 3, GUN - 1000)  # meta antes del disparo: imposible
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    assert comp.results[0].status == "RUN"
    assert any(i.code == "before_start" and i.severity == "error" for i in comp.issues)


def test_unknown_chip_counted_not_assigned():
    r = Reads()
    r.add("ZZZ", 3, GUN + 900_000)
    r.add("ZZZ", 3, GUN + 901_000)
    r.add("C1", 3, GUN + 1_000_000)
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    assert comp.unknown_chips == {"ZZZ": 2}
    assert len(comp.results) == 1 and comp.results[0].position == 1
    assert any(i.code == "unknown_chip" for i in comp.issues)


def test_extra_pass_is_info_and_first_is_used():
    r = Reads()
    r.add("C1", 3, GUN + 1_000_000)
    r.add("C1", 3, GUN + 1_100_000)  # vuelve a pasar 100 s después
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    assert comp.results[0].gun_ms == 1_000_000
    extra = [i for i in comp.issues if i.code == "extra_pass"]
    assert extra and extra[0].severity == "info"


def test_cluster_chain_within_window_is_one_pass():
    r = Reads()
    for k in range(5):  # cada lectura a 4 s de la anterior: sigue siendo un solo paso
        r.add("C1", 3, GUN + 1_000_000 + k * 4000)
    comp = compute(cfg(), POINTS, [runner(1)], r.items)
    assert not [i for i in comp.issues if i.code == "extra_pass"]
    assert comp.duplicate_reads == 4


def test_out_of_order_and_segment_too_short():
    r = Reads()
    r.add("C1", 2, GUN + 900_000)
    r.add("C1", 3, GUN + 800_000)  # meta antes que el parcial
    r.add("C2", 2, GUN + 600_000)
    r.add("C2", 3, GUN + 610_000)  # 10 s entre parcial y meta
    comp = compute(cfg(), POINTS, [runner(1), runner(2)], r.items)
    codes = {(i.bib, i.code) for i in comp.issues}
    assert (1, "out_of_order") in codes
    assert (2, "segment_too_short") in codes


def test_status_flow_by_race_state():
    runners = [runner(1)]
    assert compute(cfg(status="setup", gun_time_ms=None), POINTS, runners, []).results[0].status == "PEND"
    assert compute(cfg(status="running"), POINTS, runners, []).results[0].status == "RUN"
    closed = compute(cfg(status="closed"), POINTS, runners, [])
    assert closed.results[0].status == "NT"
    assert any(i.code == "no_finish" and i.severity == "error" for i in closed.issues)


def test_manual_status_conflict_and_no_ranking():
    r = Reads()
    r.add("C1", 3, GUN + 1_000_000)
    r.add("C2", 3, GUN + 1_100_000)
    comp = compute(cfg(), POINTS, [runner(1, status="DQ"), runner(2)], r.items)
    by_bib = {x.bib: x for x in comp.results}
    assert by_bib[1].status == "DQ" and by_bib[1].position is None
    assert by_bib[2].position == 1  # el DQ no ocupa lugar
    assert any(i.code == "status_conflict" for i in comp.issues)


def test_overrides_and_voids():
    r = Reads()
    r.add("C1", 1, GUN + 1000)
    bad = r.add("C1", 3, GUN + 10_000)  # lectura espuria
    r.add("C1", 3, GUN + 2_000_000)  # lectura buena (fuera de ventana)
    comp = compute(cfg(), POINTS, [runner(1)], r.items, voided={bad})
    assert comp.results[0].gun_ms == 2_000_000
    # Tiempo perdido agregado a mano
    comp2 = compute(cfg(), POINTS, [runner(2)], [], overrides=[Override(1, 2, 3, GUN + 1_500_000)])
    row = comp2.results[0]
    assert row.status == "FIN" and row.gun_ms == 1_500_000 and "manual" in row.flags


def test_negative_time_is_error_not_ranked():
    comp = compute(cfg(), POINTS, [runner(1)], [], overrides=[Override(1, 1, 3, GUN - 5000)])
    assert comp.results[0].status != "FIN"
    assert any(i.code == "negative_time" for i in comp.issues)


def test_ties_share_position_and_hybrid_modes():
    r = Reads()
    # Mismo tiempo de pistola; distinto neto (el 2 salió más tarde del tapete)
    r.add("C1", 1, GUN + 1000)
    r.add("C1", 3, GUN + 1_000_000)
    r.add("C2", 1, GUN + 30_000)
    r.add("C2", 3, GUN + 1_000_000)
    runners = [runner(1, cat="A"), runner(2, cat="A")]
    hybrid = {x.bib: x for x in compute(cfg(award_mode="hybrid"), POINTS, runners, r.items).results}
    assert hybrid[1].position == hybrid[2].position == 1  # general por pistola: empate
    assert hybrid[2].category_pos == 1 and hybrid[1].category_pos == 2  # categoría por neto
    gun = {x.bib: x for x in compute(cfg(award_mode="gun"), POINTS, runners, r.items).results}
    assert gun[1].category_pos == gun[2].category_pos == 1
    net = {x.bib: x for x in compute(cfg(award_mode="net"), POINTS, runners, r.items).results}
    assert net[2].position == 1 and net[1].position == 2


def test_gender_and_category_positions():
    r = Reads()
    for i, t in enumerate([600, 700, 800, 900], start=1):
        r.add(f"C{i}", 3, GUN + t * 1000)
    runners = [runner(1, "M", "M-A"), runner(2, "F", "F-A"), runner(3, "M", "M-A"), runner(4, "F", "F-A")]
    rows = {x.bib: x for x in compute(cfg(), POINTS, runners, r.items).results}
    assert [rows[b].position for b in (1, 2, 3, 4)] == [1, 2, 3, 4]
    assert [rows[b].gender_pos for b in (1, 2, 3, 4)] == [1, 1, 2, 2]
    assert [rows[b].category_pos for b in (1, 2, 3, 4)] == [1, 1, 2, 2]


def test_result_is_independent_of_read_order():
    rng = random.Random(7)
    runners = [runner(i, "M" if i % 2 else "F", "A" if i % 3 else "B") for i in range(1, 61)]
    r = Reads()
    for i in range(1, 61):
        t0 = GUN + rng.randint(0, 20_000)
        base = rng.randint(900_000, 2_400_000)
        for dt in (0, 300, 700):
            r.add(f"C{i}", 1, t0 + dt)
        if rng.random() > 0.2:
            r.add(f"C{i}", 2, GUN + base // 2)
        if rng.random() > 0.05:
            for dt in (0, 150, 900):
                r.add(f"C{i}", 3, GUN + base + dt)
    r.add("FOREIGN", 3, GUN + 1_200_000)
    ref = compute(cfg(), POINTS, runners, r.items)
    for _ in range(25):
        shuffled = r.items[:]
        rng.shuffle(shuffled)
        again = compute(cfg(), POINTS, runners, shuffled)
        assert again.results == ref.results
        assert again.issues == ref.issues
        assert again.unknown_chips == ref.unknown_chips
