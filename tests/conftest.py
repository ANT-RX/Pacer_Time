import pytest

from pacer_time import db, repo


@pytest.fixture
def conn(tmp_path):
    c = db.create_database(tmp_path / "evento.db")
    yield c
    c.close()


@pytest.fixture
def race(conn):
    """Carrera 5K con salida, un parcial y meta; 3 corredores inscritos con chip."""
    ev = repo.create_event(conn, "Carrera de prueba", "2026-11-15", "Ciudad")
    race_id = repo.create_race(conn, ev, "5K", 5000, award_mode="hybrid", splits=1)
    people = [
        ("Ana", "López", "F", "1990-05-01", "DOC1"),
        ("Beto", "Pérez", "M", "1985-03-20", "DOC2"),
        ("Carla", "Ruiz", "F", "1970-01-10", "DOC3"),
    ]
    regs = []
    for i, (fn, ln, g, bd, doc) in enumerate(people, start=1):
        regs.append(
            repo.register(
                conn, race_id,
                {"first_name": fn, "last_name": ln, "gender": g, "birth_date": bd, "document": doc},
                chip=f"E200{i:04d}", kit_delivered=True,
            )
        )
    return race_id, regs
