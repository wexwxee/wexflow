"""GDPR-минимум: маршруты и страницы политики остаются доступными."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def test_privacy_templates_compile():
    app.templates.env.get_template("account.html")
    app.templates.env.get_template("help.html")


def test_cloud_delete_route_registered():
    routes = {(getattr(r, "path", ""), tuple(getattr(r, "methods", ()) or ())) for r in app.app.routes}
    assert any(path == "/account/delete-cloud" and "POST" in methods for path, methods in routes)


def test_local_signout_only_after_cloud_confirms_deletion():
    originals = (app.account_mod.is_signed_in, app.account_mod.sign_out,
                 app.cloud_auth.delete_cloud_data)
    signed_out = []
    try:
        app.account_mod.is_signed_in = lambda: True
        app.account_mod.sign_out = lambda: signed_out.append(True)
        app.cloud_auth.delete_cloud_data = lambda: {"ok": False}
        response = app.account_delete_cloud()
        assert not signed_out
        assert "delete_error=cloud" in response.headers["location"]

        app.cloud_auth.delete_cloud_data = lambda: {"ok": True}
        response = app.account_delete_cloud()
        assert signed_out == [True]
        assert "deleted=1" in response.headers["location"]
    finally:
        (app.account_mod.is_signed_in, app.account_mod.sign_out,
         app.cloud_auth.delete_cloud_data) = originals


if __name__ == "__main__":
    tests = [test_privacy_templates_compile, test_cloud_delete_route_registered,
             test_local_signout_only_after_cloud_confirms_deletion]
    for fn in tests:
        fn()
        print(f"OK   {fn.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
