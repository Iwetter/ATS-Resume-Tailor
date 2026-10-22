"""External job-board searches and the intentional local import form."""
import streamlit as st

from .boards import import_listing, search_links


def render_boards(store, days):
    with st.expander("More job boards & import a listing", expanded=True):
        st.write("Search LinkedIn, Indeed and other boards, then bring a listing into your local tracker for resume matching and tailoring.")
        a, b = st.columns([2, 1])
        keywords = a.text_input("Board search keywords", "electrical engineering internship 2027", key="external_keywords")
        location = b.text_input("Board search location", "United States", key="external_location")
        columns = st.columns(3)
        for i, (name, url) in enumerate(search_links(keywords, location, days).items()):
            columns[i % 3].link_button("Search " + name, url, width="stretch")
        st.caption("Searches open in your browser; some boards require an account. Workday employers uses a Google site search. Adjust each site's filters there. LinkedIn and Indeed listings are imported from text you paste.")
        st.caption("Search Now automatically scans enabled employer boards on Greenhouse, Lever, Ashby, SmartRecruiters, Workday public sitemaps, Recruitee and Personio where public access is available. Add employer URLs in source settings below.")
        with st.form("import_external_listing", clear_on_submit=True):
            st.markdown("**Import a job from any board**")
            origin = st.text_input("Listing URL", placeholder="https://www.linkedin.com/jobs/view/…")
            company = st.text_input("Listing employer")
            title = st.text_input("Exact listing title")
            loc = st.text_input("Listing location", placeholder="City, state, country or the exact remote restriction")
            application = st.text_input("Employer application URL (optional)", help="Use the employer's own application page. Required if you confirm availability below.")
            published = st.text_input("Posted date (optional, YYYY-MM-DD)", help="Copy an explicit posted date. Leave blank when unknown; import time does not count as posting time.")
            description = st.text_area("Paste complete job description", height=180)
            confirmed = st.checkbox("I checked the employer application page and it is accepting applications")
            if st.form_submit_button("Import listing into Hunter", type="primary"):
                try:
                    key, fresh = import_listing(store, origin_url=origin, company=company, title=title, location=loc,
                                                description=description, application_url=application,
                                                published_date=published, confirmed=confirmed)
                    st.session_state.hunter_import_message = "Listing saved locally." if fresh else "Existing listing updated or linked; application history kept."
                    st.session_state.hunter_view = "Imported jobs"
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if message := st.session_state.pop("hunter_import_message", None):
        st.success(message + " Open it below. A verified master is required for personalized scoring and tailoring.")
