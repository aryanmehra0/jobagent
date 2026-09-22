"""Explain existing scores; never treat an absent keyword as permission to invent it."""
import re


def missing_evidence(profile, missing):
    parts = [profile.get('summary', '')]
    parts.extend(s for values in profile.get('skills', {}).values() if isinstance(values, list) for s in values if isinstance(s, str))
    for role in profile.get('experience', []):
        parts.extend(role.get('description_bullets', []))
        parts.append(role.get('title', ''))
    for project in profile.get('projects', []):
        parts += [project.get('title', ''), project.get('description', '')]
        parts.extend(project.get('technologies', []))
    text = ' '.join(parts)
    return [skill.strip() for skill in missing if skill.strip() and re.search(
        r'(?<![\w+#])' + re.escape(skill.strip()) + r'(?![\w+#])', text, re.I)]
