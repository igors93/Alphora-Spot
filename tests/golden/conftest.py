"""Golden replay update option."""


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite golden expectations for a reviewed behavior change",
    )
