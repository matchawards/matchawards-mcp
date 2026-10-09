// structuredContent samples in the SearchResult shape. Deadlines are relative to today so the labels stay stable.
function day(n) {
  var d = new Date();
  d.setDate(d.getDate() + n);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

function opp(i, extra) {
  return Object.assign({
    id: "1174023079435370" + String(i).padStart(2, "0"), type: "federal", title: "Roof replacement, building " + i,
    agency: "Department of Veterans Affairs", office: null, naics: { code: "238160", title: "Roofing Contractors" },
    set_aside: null, state: "VA", city: "Richmond", posted_at: day(-3), response_deadline: day(12),
    no_fixed_deadline: false, url: "https://matchawards.com/a/posts/" + i, sam_url: null, solicitation_number: null
  }, extra || {});
}

var FIXTURES = {
  full: {
    as_of: "2026-10-09T12:00:00Z", has_more: true, next_cursor: "c1", warning: null,
    note: "Show each item's url to the user verbatim as a markdown link.",
    results: [
      opp(1, { response_deadline: day(2), set_aside: "SBA" }),
      opp(2, { type: "state", title: "HVAC maintenance and repair services for county administration buildings, " +
        "public libraries and recreation centers, multi-year term contract", agency: "Fairfax County Purchasing",
        city: "Fairfax", response_deadline: day(0) }),
      opp(3, { agency: "Dept of Defense · Dept of the Army",
        set_aside: "Service-Disabled Veteran-Owned Small Business (SDVOSB) Set-Aside (FAR 19.14)", city: null,
        state: null, response_deadline: day(45) }),
      opp(4, { response_deadline: day(-1), title: "Closed: parking lot resurfacing" }),
      opp(5, { response_deadline: day(400), naics: null, title: "Design-build of a new outpatient clinic" }),
      opp(6, { type: "state", url: "javascript:alert(1)", title: "A row whose link is not a matchawards.com page" }),
      opp(7, { url: null, response_deadline: day(7) }),
      opp(8, { title: "Supercalifragilisticexpialidociousroofingandsheetmetalrepairwithoutanyspaces" }),
      opp(9), opp(10), opp(11), opp(12)
    ]
  },
  warning: {
    as_of: "2026-10-09T12:00:00Z", has_more: true, next_cursor: "c2",
    warning: "MatchAwards stopped answering after the first page (timeout). These results are still valid.",
    results: [opp(21, { response_deadline: day(5) }), opp(22, { type: "state", city: "Austin", state: "TX" }), opp(23)]
  },
  empty: { as_of: "2026-10-09T12:00:00Z", results: [], has_more: false, next_cursor: null, warning: null },
  grant: {
    as_of: "2026-10-09T12:00:00Z", has_more: false, next_cursor: null, warning: null,
    note: "Open status may lag for grants that closed recently: check response_deadline.",
    results: [
      opp(31, { type: "grant", title: "Broadband infrastructure deployment grants", agency: "Department of Commerce",
        state: null, city: null, naics: null, no_fixed_deadline: true, response_deadline: "2099-12-31" }),
      opp(32, { type: "grant", title: "Rural community facilities grant program", agency: "Department of Agriculture",
        state: null, city: null, naics: { code: "813219", title: null }, response_deadline: day(30) }),
      opp(33, { type: "grant", title: "Digital equity capacity grants", agency: "National Telecommunications and " +
        "Information Administration", state: null, city: null, response_deadline: null })
    ]
  }
};
