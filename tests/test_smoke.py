def test_imports():
    import importlib
    for mod in ["main", "utils.http", "telegram.api"]:
        importlib.import_module(mod)

def test_dummy():
    assert True
