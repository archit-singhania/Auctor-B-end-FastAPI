"""Readable, paginated evidence exports using the Auctor visual identity."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


INK = colors.HexColor('#202e32')
JADE = colors.HexColor('#275d57')
BRONZE = colors.HexColor('#806239')
PAPER = colors.HexColor('#fffcf6')
MUTED = colors.HexColor('#526568')


def evidence_pdf(data, user):
    """Export only the same contact-redacted evidence payload as JSON."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=44, leftMargin=44,
                           topMargin=58, bottomMargin=58,
                           title='Auctor Evidence Report', author='Auctor')
    body = ParagraphStyle('body', fontName='Helvetica', fontSize=10,
                          leading=16, textColor=INK, spaceAfter=10)
    display = ParagraphStyle('display', fontName='Times-Roman', fontSize=29,
                             leading=34, textColor=INK, spaceAfter=8)
    caption = ParagraphStyle('caption', parent=body, fontSize=9,
                             leading=14, textColor=MUTED)
    section = ParagraphStyle('section', fontName='Helvetica-Bold', fontSize=11,
                             leading=16, textColor=JADE, spaceBefore=22, spaceAfter=12,
                             keepWithNext=True)
    score_style = ParagraphStyle('score', fontName='Times-Roman', fontSize=30,
                                 leading=36, textColor=JADE)
    text = lambda value: escape(str(value))

    def page(canvas, document):
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(PAPER)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setFillColor(JADE)
        canvas.setFont('Helvetica-Bold', 10)
        canvas.drawString(44, height - 30, 'AUCTOR')
        canvas.setFillColor(BRONZE)
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(width - 44, height - 30, 'PROOF OF YOUR CRAFT')
        canvas.setStrokeColor(colors.HexColor('#d9dfd8'))
        canvas.line(44, 44, width - 44, 44)
        canvas.setFillColor(MUTED)
        canvas.setFont('Helvetica', 8)
        canvas.drawString(44, 29, 'Evidence report  |  Formula v1')
        canvas.drawRightString(width - 44, 29, f'{document.page}')
        canvas.restoreState()

    story = [Paragraph('Evidence report', display),
             Paragraph(text(user['display_name']) + ' &nbsp; @' + text(user['handle']), body),
             Spacer(1, 16)]
    score = Table([[Paragraph(text(data['score']['total']) + ' / 10', score_style),
                    Paragraph('AUCTOR SCORE<br/>Five weighted evidence signals<br/>Explainable formula v1', caption)]],
                  colWidths=[160, A4[0] - 88 - 160])
    score.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#e6eeec')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 16),
        ('RIGHTPADDING', (0, 0), (-1, -1), 16),
        ('TOPPADDING', (0, 0), (-1, -1), 16),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
    ]))
    story.extend([score, Spacer(1, 14),
                  Paragraph('Evidence reflects sources and reviewer decisions, not a hiring guarantee.', caption),
                  Paragraph('SKILLS', section),
                  Paragraph(text(', '.join(data['skills']) or 'No skills recorded.'), body),
                  Paragraph('EVIDENCE & PROVENANCE', section)])
    for evidence in data['evidence']:
        story.append(Paragraph('<b>' + text(evidence['title']) + '</b><br/>' +
                               text(evidence['kind']) + ' &nbsp; / &nbsp; ' + text(evidence['status']), body))
    if not data['evidence']:
        story.append(Paragraph('No evidence recorded.', caption))
    story.append(Paragraph('ASSESSED BADGES', section))
    story.append(Paragraph(text(', '.join(data['badges']) or 'No assessed badges earned.'), body))
    doc.build(story, onFirstPage=page, onLaterPages=page)
    return buffer.getvalue()
