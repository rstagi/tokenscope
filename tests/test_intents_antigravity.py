import unittest
from tokenscope import intents

class TestIntentsCompatibility(unittest.TestCase):
    def test_claude_intents(self):
        # Read code vs doc
        r_code = intents.record("Read", {"file_path": "main.py"})
        self.assertEqual(intents.tool_intent(r_code), "read_code")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_code)], "investigate")

        r_doc = intents.record("Read", {"file_path": "README.md"})
        self.assertEqual(intents.tool_intent(r_doc), "read_docs")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_doc)], "investigate")

        # Edit
        r_edit = intents.record("Edit", {"file_path": "main.py"})
        self.assertEqual(intents.tool_intent(r_edit), "edit")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_edit)], "change")

        # Bash
        r_bash_test = intents.record("Bash", {"command": "pytest"})
        self.assertEqual(intents.tool_intent(r_bash_test), "test")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_bash_test)], "verify")

        r_bash_grep = intents.record("Bash", {"command": "grep -rn foo ."})
        self.assertEqual(intents.tool_intent(r_bash_grep), "locate")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_bash_grep)], "investigate")

    def test_antigravity_intents(self):
        # read_file
        r_code = intents.record("read_file", {"Path": "/path/to/script.py"})
        self.assertEqual(intents.tool_intent(r_code), "read_code")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_code)], "investigate")

        r_doc = intents.record("read_file", {"Path": "/path/to/doc.md"})
        self.assertEqual(intents.tool_intent(r_doc), "read_docs")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_doc)], "investigate")

        # edit_file & write_file
        r_edit = intents.record("edit_file", {"Path": "/path/to/file.py"})
        self.assertEqual(intents.tool_intent(r_edit), "edit")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_edit)], "change")

        r_write = intents.record("write_file", {"Path": "/path/to/file.py"})
        self.assertEqual(intents.tool_intent(r_write), "edit")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_write)], "change")

        # run_command
        r_test = intents.record("run_command", {"CommandLine": "python3 -m unittest"})
        self.assertEqual(intents.tool_intent(r_test), "test")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_test)], "verify")

        r_locate = intents.record("run_command", {"CommandLine": "find . -name '*.py'"})
        self.assertEqual(intents.tool_intent(r_locate), "locate")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_locate)], "investigate")

        # search_web
        r_search = intents.record("search_web", {"query": "python docs"})
        self.assertEqual(intents.tool_intent(r_search), "research")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_search)], "research")

        # invoke_subagent
        r_sub = intents.record("invoke_subagent", {"Role": "Researcher", "TypeName": "research"})
        self.assertEqual(intents.tool_intent(r_sub), "delegate")
        self.assertEqual(intents.GROUP[intents.tool_intent(r_sub)], "delegate")

if __name__ == "__main__":
    unittest.main()
