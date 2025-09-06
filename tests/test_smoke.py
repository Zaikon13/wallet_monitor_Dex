# tests/test_smoke.py

def test_imports():
    import importlib
    for mod in [
        "main",
        "utils.http",
        "telegram.api",
    ]:
        importlib.import_module(mod)


def test_main_has_entrypoint():
    import main
    assert hasattr(main, "main"), "Το main.py πρέπει να έχει συνάρτηση main()"
