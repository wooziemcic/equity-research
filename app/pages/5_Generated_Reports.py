from __future__ import annotations

from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.pages._placeholder import render_phase_placeholder


def main() -> None:
    bootstrap_page("PM Approval")
    render_sidebar()
    render_phase_placeholder(
        page_name="Generated Reports",
        phase="Future Phase",
        future_capabilities=[
            "Draft investment report output after package lock and analysis.",
            "Portfolio-manager approval queue.",
            "Final research package downloads after review.",
        ],
    )


if __name__ == "__main__":
    main()
