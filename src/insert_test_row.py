from src.config import load_config
from src.storage.database import get_session, init_db, set_database_url
from src.storage.models import JobPost

config = load_config()
set_database_url(config.database.url)
init_db()

with get_session() as session:
    session.add(JobPost(
        job_board_id="test-404",   # must be unique — this is your marker
        title="Fake Dead Job",
        company="Test Co",
        location="Nowhere",
        description="Row inserted to test dead-link detection.",
        url="https://httpstat.us/404",
    ))
    session.commit()
print("Inserted test row.")
