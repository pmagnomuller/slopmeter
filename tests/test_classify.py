from slopmeter.scoring import RepoConfig, classify_file, group_of, is_generated, set_repo_config


def teardown_function(_):
    set_repo_config(RepoConfig())


def test_prod_source():
    assert classify_file("services/billing/main.go") == "prod"
    assert classify_file("scripts/deploy.sh") == "prod"
    assert classify_file("terraform/main.tf") == "prod"


def test_tests_by_dir_and_name():
    assert classify_file("tests/unit/test_foo.py") == "test"
    assert classify_file("services/billing/handler_test.go") == "test"
    assert classify_file("web/src/app.spec.ts") == "test"
    assert classify_file("tests/bdd/login.feature") == "test"
    assert classify_file("mocks/inventory-api/inventory_api.py") == "test"
    assert classify_file("services/x/conftest.py") == "test"


def test_ignored_even_under_tests():
    assert classify_file("tests/fixtures/data.json") == "ignored"
    assert classify_file("docs/README.md") == "ignored"
    assert classify_file("bruno/collection.bru") == "ignored"
    assert classify_file("go.sum") == "ignored"
    assert classify_file("node_modules/x/index.js") == "ignored"


def test_generated_ignored():
    assert is_generated("api/v1/service.pb.go")
    assert is_generated("internal/mocks/mock_store.go")
    assert classify_file("api/v1/service.pb.go") == "ignored"
    assert classify_file("lib/types_pb2.py") == "ignored"


def test_repo_config_overrides():
    set_repo_config(RepoConfig(ignore=["docs/**", "**/*.sql"], test=["qa/**"], prod=["mocks/server/**"]))
    assert classify_file("services/db/migrations/001.sql") == "ignored"
    assert classify_file("qa/smoke.py") == "test"
    assert classify_file("mocks/server/main.py") == "prod"


def test_group_of():
    assert group_of("README.md") == "(root)"
    assert group_of("scripts/x/y.sh") == "scripts"
    assert group_of("services/billing/main.go") == "services/billing"
    assert group_of("services/billing/main.go", depth=2) == "services/billing"
    assert group_of("a/b/c/d.go", depth=2) == "a/b"
