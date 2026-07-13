from __future__ import annotations

from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.pages._placeholder import render_phase_placeholder


def main() -> None:
    bootstrap_page("Investment Analysis")
    render_sidebar()
    render_phase_placeholder(
        page_name="Investment Analysis",
        phase="Future Phase",
        future_capabilities=[
            "Evidence-bound Buy, Hold, Sell, or Insufficient Evidence analysis.",
            "Document-level and page-level citation generation.",
            "Analyst review workflow before PM approval.",
        ],
    )


if __name__ == "__main__":
    main()
