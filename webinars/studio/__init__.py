"""Marketer studio at /studio/: Django templates + vanilla JS/CSS, no build step.

Access is session-based (Django auth) and every view is guarded by
``webinars.studio.access``. Content pages live in ``urls_content``/``views_content``;
settings pages (integrations, API keys, team, mailbox, account) in
``urls_settings``/``views_settings``.
"""
