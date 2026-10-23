"""Autonomous discovery → tailoring → application pipeline.

Safety model, in order of precedence:

1. Global emergency stop (database flag, ``data/STOP`` file or
   ``AUTOAPPLY_EMERGENCY_STOP``) halts every application action.
2. ``AUTOAPPLY_ALLOW_REAL_SUBMISSION`` must be ``true`` for any real submit
   click. It is ``false`` by default and in every test.
3. The master switch, the mode (OFF / REVIEW / AUTO, default REVIEW) and
   per-employer permissions decide what the worker may prepare or submit.
4. Hard eligibility gates, a verified applicant profile and verified answers
   are required; unknowns become Needs Human Action, never guesses.
"""
