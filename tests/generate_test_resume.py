"""Helper script to generate a sample ATS-friendly resume PDF for testing."""

from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def generate_sample_pdf(output_path: Path) -> Path:
    """Generate a clean single-column test resume PDF."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1e293b"),
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#475569"),
    )
    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=18,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=12,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#334155"),
    )
    bullet_style = ParagraphStyle(
        "Bullet",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13.5,
        textColor=colors.HexColor("#334155"),
        leftIndent=15,
        bulletIndent=5,
    )

    story = []

    # Header
    story.append(Paragraph("Alex Rivera", title_style))
    story.append(
        Paragraph(
            "San Francisco, CA | alex.rivera@example.com | +1 (415) 555-0142 | linkedin.com/in/alexrivera | github.com/alexrivera",
            subtitle_style,
        )
    )
    story.append(Spacer(1, 10))

    # Summary
    story.append(Paragraph("PROFESSIONAL SUMMARY", heading_style))
    story.append(
        Paragraph(
            "Senior Distributed Systems and Cloud Engineer with 5+ years of experience building resilient microservices, high-throughput streaming systems, and cloud-native infrastructure.",
            body_style,
        )
    )

    # Experience
    story.append(Paragraph("WORK EXPERIENCE", heading_style))
    story.append(Paragraph("<b>ScaleFlow Technologies</b> — Lead Infrastructure Engineer (2022 - Present)", body_style))
    story.append(Paragraph("• Architected multi-region Kubernetes clusters supporting over 200k RPS with 99.999% SLA availability.", bullet_style))
    story.append(Paragraph("• Reduced AWS infrastructure costs by $340k annually by deploying intelligent spot instance auto-scalers.", bullet_style))
    story.append(Paragraph("• Decreased p99 API query latency by 45% using distributed Redis caching and gRPC streaming.", bullet_style))
    story.append(Paragraph("• Led an engineering squad of 6 engineers across CI/CD and production site reliability initiatives.", bullet_style))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>CloudPulse Inc</b> — Senior Software Engineer (2020 - 2022)", body_style))
    story.append(Paragraph("• Designed high-scale event ingestion pipeline processing 50M events daily via Apache Kafka and Go.", bullet_style))
    story.append(Paragraph("• Zero-downtime migration of multi-terabyte transactional database from PostgreSQL to CockroachDB.", bullet_style))
    story.append(Paragraph("• Optimized batch ingestion throughput by 3.5x using parallel Goroutines and memory pools.", bullet_style))
    story.append(Spacer(1, 10))

    # Education
    story.append(Paragraph("EDUCATION", heading_style))
    story.append(Paragraph("<b>Stanford University</b> — M.S. in Computer Science (2018 - 2020)", body_style))
    story.append(Paragraph("<b>UC Berkeley</b> — B.S. in Electrical Engineering & Computer Sciences (2014 - 2018)", body_style))
    story.append(Spacer(1, 10))

    # Skills
    story.append(Paragraph("TECHNICAL SKILLS", heading_style))
    story.append(Paragraph("<b>Languages:</b> Python, Go, Rust, TypeScript, SQL, Bash", body_style))
    story.append(Paragraph("<b>Cloud & DevOps:</b> AWS, Kubernetes, Docker, Terraform, Helm, GitHub Actions", body_style))
    story.append(Paragraph("<b>Databases & Messaging:</b> PostgreSQL, CockroachDB, Redis, Apache Kafka, DynamoDB", body_style))
    story.append(Paragraph("<b>Architecture:</b> Distributed Systems, Microservices, gRPC, Event-Driven Systems, High Availability", body_style))

    doc.build(story)
    return output_path


if __name__ == "__main__":
    out = Path("data/raw_resumes/sample_resume.pdf")
    generate_sample_pdf(out)
    print(f"Generated sample resume PDF at {out}")

