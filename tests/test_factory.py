def test_factory_reexports_planner():
    # factory.py is a deprecation shim; it should still re-export the planner API.
    from src.ingestion.factory import PlannedScrape, plan_scrapes

    assert callable(plan_scrapes)
    assert PlannedScrape is not None
