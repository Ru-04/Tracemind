import pytest
from fastapi.testclient import TestClient

from demo_app.main import app
from demo_app.scenarios import SCENARIOS, Scenario

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_PAYMENT_GATEWAY_URL", raising=False)


def test_health_is_ok() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_scenarios_are_listed() -> None:
    listed = client.get("/demo/scenarios").json()
    assert [s["id"] for s in listed] == [s.id for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_scenario_raises_expected_error_every_time(scenario: Scenario) -> None:
    for _ in range(3):
        with pytest.raises(Exception) as info:  # noqa: B017 - the type is asserted below
            scenario.run()
        assert type(info.value).__name__ == scenario.expected_error_type


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_endpoint_returns_generic_500_without_leaking_details(scenario: Scenario) -> None:
    response = client.get(scenario.route)
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert scenario.expected_error_type not in response.text


def test_config_error_clears_when_setting_is_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PAYMENT_GATEWAY_URL", "https://gateway.example.test")
    # The config scenario is the only one that is environment-dependent.
    from demo_app.scenarios import config_error

    assert config_error() == "https://gateway.example.test"
