"""Contact discovery: published emails for sending an application or resume.

Only addresses that are actually published are collected — in the job post, on
the company's own careers or contact pages, or returned by a lookup service the
user has configured. Nothing is guessed: a pattern-generated address such as
`firstname.lastname@company.com` is unverified, and sending a resume to it would
mean mailing a stranger. Every contact records where it was found.
"""

from job_agent.contacts.extract import classify_email, extract_emails

__all__ = ["classify_email", "extract_emails"]
