import unittest

from microtask_monitor.employer_replies import draft_reply


PROFILE = {
    "contact": {"full_name": "Sample Candidate", "linkedin": "https://example.com/linkedin"},
    "portfolio": {"youtube_channel": "https://example.com/portfolio"},
    "availability": {"hours_per_day": 6, "time_zone": "Africa/Nairobi"},
    "established_skills": {
        "programming": ["Python", "JavaScript"],
        "development": ["Backend development"],
        "ai_video_and_design": ["Adobe Premiere Pro (Advanced)", "Adobe After Effects (Advanced)"],
        "automation": ["n8n", "Make.com"],
        "ai_workflows": ["Prompt engineering"],
        "ai_assistants": ["ChatGPT", "Claude"],
        "writing": ["Blog writing"],
    },
}


class EmployerReplyDraftTests(unittest.TestCase):
    def test_availability_uses_only_saved_hours_and_timezone(self):
        status, category, body = draft_reply("Availability", "How many hours per day can you work?", PROFILE)
        self.assertEqual((status, category), ("auto_reply", "general_availability"))
        self.assertIn("6 hours per day", body)
        self.assertIn("Africa/Nairobi", body)

    def test_interview_reply_does_not_choose_a_slot(self):
        status, category, body = draft_reply("Interview invitation", "We would like to interview you.", PROFILE)
        self.assertEqual((status, category), ("auto_reply", "interview_interest_not_scheduling"))
        self.assertIn("share a few available interview dates", body)

    def test_portfolio_uses_only_saved_public_links(self):
        status, category, body = draft_reply("Please share your portfolio", "Can you send work samples?", PROFILE)
        self.assertEqual((status, category), ("auto_reply", "portfolio_links"))
        self.assertIn("https://example.com/linkedin", body)
        self.assertIn("https://example.com/portfolio", body)

    def test_unclassified_question_is_held(self):
        status, category, _ = draft_reply("Application follow-up", "Please review the attached details.", PROFILE)
        self.assertEqual((status, category), ("human_hold", "unclassified_employer_question"))


if __name__ == "__main__":
    unittest.main()
