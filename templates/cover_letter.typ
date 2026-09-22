#let data_file = sys.inputs.at("data_file", default: "resume_data.json")
#let data = json(data_file)
#set page(paper: data.paper, margin: 22mm)
#set text(font: "Arial", size: 11pt)
#set par(leading: 0.65em)
#align(center)[#text(size: 17pt, weight: "bold", data.contact.full_name) \
  #data.contact.email]
#v(1cm)
#strong(data.role)
#parbreak()
#data.company
#v(0.6cm)
Dear Hiring Team,
#for paragraph in data.paragraphs [
  #parbreak()
  #paragraph
]
#parbreak()
Kind regards, \
#data.contact.full_name
