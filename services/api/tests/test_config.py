from sqlalchemy.engine import make_url

from app.config import Settings


def test_asyncpg_url_drops_postgres_client_only_options() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://user:password@db.example.test/app?sslmode=require&channel_binding=require&schema=public"
        ),
    )

    url = make_url(settings.sqlalchemy_database_url)

    assert url.drivername == "postgresql+asyncpg"
    assert url.query == {"sslmode": "require"}
