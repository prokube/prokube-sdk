from pathlib import Path

from prokube.cli import manpage

MAN_PAGE = Path(__file__).parents[1] / "man" / "pk.1"


def test_the_man_page_is_current():
    assert manpage.render() == MAN_PAGE.read_text(), (
        "man/pk.1 is out of date. Run: python -m prokube.cli.manpage > man/pk.1"
    )


def test_the_man_page_has_every_command():
    page = MAN_PAGE.read_text()

    for command in ("pk sandbox create", "pk sandbox exec", "pk auth workspace set"):
        assert f'.SS "{command} [OPTIONS]' in page.replace("\\-", "-")
