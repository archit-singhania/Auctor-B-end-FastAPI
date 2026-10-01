"""Generate labelled, synthetic PDFs for local manual QA; no database/provider calls."""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas


def pdf(path, lines):
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle('Auctor local QA fixture — synthetic input')
    canvas.setFont('Helvetica-Bold', 16)
    canvas.drawString(48, 790, 'AUCTOR LOCAL QA — SYNTHETIC FIXTURE')
    canvas.setFont('Helvetica', 11)
    y = 755
    for line in lines:
        canvas.drawString(48, y, line)
        y -= 23
    canvas.save()


def main():
    output = Path(__file__).resolve().parents[1] / '_data' / 'manual-fixtures'
    output.mkdir(parents=True, exist_ok=True)
    pdf(output / 'synthetic-cv.pdf', [
        'QA Developer — generated local test input, not a real candidate',
        'Email: qa-developer@example.test',
        'Skills: Docker, REST API, PostgreSQL, Redis, Python, JWT',
        'Projects',
        'QA Orders API — synthetic Docker and PostgreSQL service',
        'Experience',
        'QA Fixture Company — Software Engineer — Jan 2025 to Jan 2026',
        'All employment, project and skill statements above are synthetic.',
        'No GitHub ownership, certification or work history is asserted.',
    ])
    pdf(output / 'synthetic-proof.pdf', [
        'Synthetic proof document for testing upload and reviewer workflow.',
        'This document is not an employment letter or certificate.',
        'A local reviewer may mark it reviewed only as a software-test fixture.',
    ])
    (output / 'invalid.pdf').write_bytes(b'This is deliberately not a PDF.')
    (output / 'damaged.pdf').write_bytes(b'%PDF-1.7\nDeliberately damaged local QA input\n')
    # Valid blank PDF demonstrates the no-extractable-text failure path.
    empty = Canvas(str(output / 'blank.pdf'), pagesize=A4)
    empty.showPage()
    empty.save()
    print('Created five synthetic local QA files under _data/manual-fixtures.')


if __name__ == '__main__':
    main()
