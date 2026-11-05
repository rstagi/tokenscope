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

    def test_antigravity_step_summarization(self):
        from tokenscope import steps as stepsmod

        # Test display_name
        self.assertEqual(stepsmod.display_name("run_command", {"CommandLine": "python3 -m unittest"}), "run_command unittest")
        self.assertEqual(stepsmod.display_name("invoke_subagent", {"Role": "Researcher"}), "Agent(Researcher)")

        # Turn with toolSummary
        turns = [{
            "ts": 1000.0, "cw": 500.0, "usd": 0.001,
            "tools": [{
                "name": "run_command",
                "input": {"CommandLine": "python3 -c \"import evdev\"", "toolSummary": "Check python3 evdev module"}
            }]
        }]
        steps = stepsmod.segment(turns)
        self.assertEqual(len(steps), 1)
        st = steps[0]
        self.assertEqual(st["cmds"], ["python3"])
        self.assertEqual(st["summaries"], ["Check python3 evdev module"])
        lab, src = stepsmod.label_from_signals(st)
        self.assertEqual(lab, "Check python3 evdev module")
        self.assertEqual(src, "summary")

        # Labeller locks summary
        labeller = stepsmod.Labeller()
        labeller.assign(steps)
        self.assertEqual(st["label"], "Check python3 evdev module")
        self.assertEqual(st["source"], "summary")

        # Turn without toolSummary uses rule_label with extracted command/file targets
        turns_nosum = [{
            "ts": 1001.0, "cw": 500.0, "usd": 0.001,
            "tools": [{
                "name": "run_command",
                "input": {"CommandLine": "pytest tests/test_foo.py"}
            }]
        }]
        steps2 = stepsmod.segment(turns_nosum)
        st2 = steps2[0]
        self.assertEqual(st2["cmds"], ["pytest"])
        lab2, src2 = stepsmod.label_from_signals(st2)
        self.assertIsNone(lab2)  # no explicit signal
        rule_lbl = stepsmod.rule_label(st2)
        self.assertEqual(rule_lbl, "running tests · pytest")

        # Edit file turn without toolSummary
        turns_edit = [{
            "ts": 1002.0, "cw": 500.0, "usd": 0.001,
            "tools": [{
                "name": "edit_file",
                "input": {"Path": "/repo/config.py"}
            }]
        }]
        steps3 = stepsmod.segment(turns_edit)
        st3 = steps3[0]
        self.assertEqual(st3["files"], ["config.py"])
        self.assertEqual(stepsmod.rule_label(st3), "editing files · config.py")

    def test_bash_what_and_command_summaries(self):
        from tokenscope import steps as stepsmod, intents
        from tokenscope.live import state as livestate

        cases = [
            ("python3 train.py", "python3 train.py", "python3 train.py", "python3", "running a script · python3 train.py"),
            ("python3 -m unittest", "unittest", "unittest", "unittest", "running tests · unittest"),
            ("python3 -m http.server", "python3 -m http.server", "python3 -m http.server", "python3 -m", "running a script · python3 -m http.server"),
            ("python3 -c \"print(1)\"", "python3", "python3 -c", "python3 -c", "running a script · python3"),
            ("bash setup.sh", "bash setup.sh", "bash setup.sh", "bash", "running a build/command · bash setup.sh"),
            ("sh run.sh", "sh run.sh", "sh run.sh", "sh", "running a build/command · sh run.sh"),
            ("pip install -e .", "pip install", "pip install", "pip install", "running a build/command · pip install"),
            ("systemctl restart nginx", "systemctl restart", "systemctl restart", "systemctl restart", "running a shell command · systemctl restart"),
            ("git status", "git status", "git status", "git status", "checking git state · git status"),
            ("docker compose up", "docker compose", "docker compose", "docker compose", "running a build/command · docker compose"),
        ]

        for cmd, want_prog, want_unred, want_red, want_rule in cases:
            # program()
            self.assertEqual(stepsmod.program(cmd), want_prog)

            # bash_what()
            rec = intents.record("Bash", {"command": cmd})
            self.assertEqual(livestate.bash_what(rec, redact=False), want_unred)
            self.assertEqual(livestate.bash_what(rec, redact=True), want_red)

            # rule_label() on Bash turn
            t_bash = {"ts": 1000.0, "cw": 10.0, "usd": 0.0001, "tools": [{"name": "Bash", "input": {"command": cmd}}]}
            st_b = stepsmod.segment([t_bash], open_last=False)[0]
            self.assertEqual(stepsmod.rule_label(st_b), want_rule)

            # rule_label() on run_command turn
            t_rc = {"ts": 1000.0, "cw": 10.0, "usd": 0.0001, "tools": [{"name": "run_command", "input": {"CommandLine": cmd}}]}
            st_rc = stepsmod.segment([t_rc], open_last=False)[0]
            self.assertEqual(stepsmod.rule_label(st_rc), want_rule)

        # toolSummary overrides in LiveState tool_target
        rec_rc = intents.record("run_command", {"CommandLine": "make build"})
        inp_sum = {"CommandLine": "make build", "toolSummary": "Package build"}
        self.assertEqual(livestate.tool_target("run_command", inp_sum, rec_rc, redact=False), "Package build")
        self.assertEqual(livestate.tool_target("run_command", inp_sum, rec_rc, redact=True), "make build")

        inp_nosum = {"CommandLine": "make build"}
        self.assertEqual(livestate.tool_target("run_command", inp_nosum, rec_rc, redact=False), "make build")

if __name__ == "__main__":
    unittest.main()
