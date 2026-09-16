// ==============================================================================
// ATS-Optimized Single-Column Resume Template (Typst)
// Single-pass compilation (<100ms), no multi-column scrambling, text-searchable
// ==============================================================================

#let data_file = sys.inputs.at("data_file", default: "resume_data.json")
#let d = json(data_file)

#set page(
  paper: "us-letter",
  margin: (x: 1.35cm, top: 1.25cm, bottom: 1.25cm),
)

#set text(
  font: ("Liberation Sans", "Helvetica", "Arial", "Roboto"),
  size: 9.5pt,
  fill: rgb("#1e293b"),
  lang: "en",
)

#set par(justify: true, leading: 0.52em)

// Helper for section headings
#let section_heading(title) = [
  #v(3pt)
  #text(10.5pt, weight: "bold", fill: rgb("#0f172a"))[#upper(title)]
  #v(-3pt)
  #line(length: 100%, stroke: 0.6pt + rgb("#94a3b8"))
  #v(1.5pt)
]

// --- HEADER ---
#align(center)[
  #text(18pt, weight: "bold", fill: rgb("#0f172a"))[#d.contact.full_name] \
  #v(1pt)
  #text(8.5pt, fill: rgb("#475569"))[
    #if d.contact.at("location", default: none) != none [#d.contact.location]
    #if d.contact.at("email", default: none) != none [ | #d.contact.email ]
    #if d.contact.at("phone", default: none) != none [ | #d.contact.phone ]
    #if d.contact.at("linkedin_url", default: none) != none [ | #link(d.contact.linkedin_url)[LinkedIn] ]
    #if d.contact.at("github_url", default: none) != none [ | #link(d.contact.github_url)[GitHub] ]
    #if d.contact.at("portfolio_url", default: none) != none [ | #link(d.contact.portfolio_url)[Portfolio] ]
  ]
]

// --- PROFESSIONAL SUMMARY ---
#section_heading("Professional Summary")
#d.summary

// --- TECHNICAL SKILLS ---
#section_heading("Technical Skills")
#if d.skills.at("languages", default: ()).len() > 0 [
  - *Languages:* #d.skills.languages.join(", ")
]
#if d.skills.at("frameworks", default: ()).len() > 0 [
  - *Frameworks & Libraries:* #d.skills.frameworks.join(", ")
]
#if d.skills.at("cloud_devops", default: ()).len() > 0 [
  - *Cloud & Infrastructure:* #d.skills.cloud_devops.join(", ")
]
#if d.skills.at("developer_tools", default: ()).len() > 0 [
  - *Databases, Storage & Tools:* #d.skills.developer_tools.join(", ")
]
#if d.skills.at("domain_knowledge", default: ()).len() > 0 [
  - *Core Competencies:* #d.skills.domain_knowledge.join(", ")
]

// --- WORK EXPERIENCE ---
#section_heading("Professional Experience")
#for exp in d.experience [
  #grid(
    columns: (1fr, auto),
    [*#exp.company* — #text(style: "italic")[#exp.title]],
    [#text(size: 8.5pt, fill: rgb("#475569"))[#exp.start_date – #exp.at("end_date", default: "Present")]],
  )
  #if exp.at("location", default: none) != none [
    #text(size: 8pt, fill: rgb("#64748b"))[#exp.location] \
  ]
  #v(1pt)
  #for bullet in exp.description_bullets [
    - #bullet
  ]
  #v(2.5pt)
]

// --- EDUCATION ---
#if d.at("education", default: ()).len() > 0 [
  #section_heading("Education")
  #for edu in d.education [
    #grid(
      columns: (1fr, auto),
      [*#edu.institution* — #edu.degree in #edu.field_of_study],
      [#text(size: 8.5pt, fill: rgb("#475569"))[#edu.at("end_date", default: "")]],
    )
    #if edu.at("gpa", default: none) != none or edu.at("honors", default: ()).len() > 0 [
      #text(size: 8pt, fill: rgb("#64748b"))[
        #if edu.gpa != none [GPA: #edu.gpa]
        #if edu.honors.len() > 0 [ | Honors: #edu.honors.join(", ")]
      ] \
    ]
    #v(1.5pt)
  ]
]

// --- KEY PROJECTS ---
#if d.at("projects", default: ()).len() > 0 [
  #section_heading("Key Projects")
  #for proj in d.projects [
    #grid(
      columns: (1fr, auto),
      [*#proj.title* #if proj.at("technologies", default: ()).len() > 0 [ — (#proj.technologies.join(", "))]],
      [#if proj.at("link", default: none) != none [#link(proj.link)[#text(size: 8pt, fill: rgb("#2563eb"))[Link]]]],
    )
    #if proj.at("role", default: none) != none [
      #text(size: 8pt, fill: rgb("#64748b"))[#proj.role]
    ]
    - #proj.description
    #v(1.5pt)
  ]
]

// --- CERTIFICATIONS ---
#if d.at("certifications", default: ()).len() > 0 [
  #section_heading("Certifications")
  #for cert in d.certifications [
    #grid(
      columns: (1fr, auto),
      [*#cert.name* — #cert.issuer],
      [#if cert.at("issue_date", default: none) != none [#text(size: 8.5pt, fill: rgb("#475569"))[#cert.issue_date]]],
    )
    #v(1pt)
  ]
]
