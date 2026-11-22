"""Responsive, accessible, and permission-aware top navigation checks."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"
environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
template = environment.get_template("base.html")
styles = (STATIC / "styles.css").read_text(encoding="utf-8")
navigation = (STATIC / "navigation.js").read_text(encoding="utf-8")
appearance = {
    "slug": "system", "scheme": "light dark", "style": "", "tokens": {"bg": "#f5f5f2"},
}


class User:
    def __init__(self, permissions=(), *, guest=False, manager=False, name="Player"):
        self.id = 7
        self.display_name = name
        self.is_guest = guest
        self.can_manage = manager
        self.permissions = set(permissions)

    def can(self, permission):
        return permission in self.permissions


def render(user):
    return template.render(
        appearance=appearance,
        current_user=user,
        hide_app_chrome=False,
        print_pdf_css=styles,
    )


def activate(page, user):
    page.set_content(render(user))
    page.add_script_tag(content=navigation)
    page.evaluate("document.addEventListener('click', event => { if (event.target.closest('a')) event.preventDefault(); }, true)")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    admin = User({"puzzles.generate"}, manager=True, name="Administrator")
    activate(page, admin)

    toggle = page.locator("[data-navigation-toggle]")
    menu = page.locator("[data-navigation-menu]")
    assert toggle.is_hidden()
    assert menu.is_visible()
    for label in ("Group Solve", "Generate", "Admin", "Offline", "Account"):
        assert menu.get_by_role("link", name=label, exact=True).count() == 1, label
    assert menu.get_by_role("button", name="Log out", exact=True).count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("!document.querySelector('[data-navigation-toggle]').hidden")
    assert toggle.is_visible() and menu.is_hidden()
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true" and menu.is_visible()
    page.emulate_media(color_scheme="dark")
    menu_colors = menu.evaluate("node => [getComputedStyle(node).backgroundColor, getComputedStyle(node).color]")
    assert menu_colors[0] not in ("transparent", "rgba(0, 0, 0, 0)") and menu_colors[0] != menu_colors[1]
    page.emulate_media(color_scheme="light")
    for item in menu.locator("a,button:not([hidden])").all():
        assert item.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.mouse.click(8, 600)
    assert menu.is_hidden() and toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    page.keyboard.press("Escape")
    assert menu.is_hidden() and toggle.evaluate("node => document.activeElement === node")
    toggle.click()
    menu.get_by_text("Group Solve", exact=True).click()
    assert menu.is_hidden()

    restricted = User(name="Reader")
    activate(page, restricted)
    assert page.get_by_text("Generate", exact=True).count() == 0
    assert page.get_by_text("Admin", exact=True).count() == 0
    for label in ("Group Solve", "Offline", "Account", "Log out"):
        assert page.get_by_text(label, exact=True).count() == 1, label

    guest = User(guest=True, name="Guest")
    activate(page, guest)
    assert page.get_by_text("Group Solve", exact=True).count() == 0
    assert page.get_by_text("Account", exact=True).count() == 0
    assert page.get_by_text("Guest (guest)", exact=True).count() == 1
    assert not errors, errors
    browser.close()

print("Responsive top navigation, disclosure behavior, touch targets, and role visibility passed.")
