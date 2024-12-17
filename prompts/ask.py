#!/usr/bin/env python3

"""
This is a script to interact with llama.cpp models. It is a wrapper around the
llama.cpp binary that allows you to easily infer prompts and get responses. It
also allows you to use presets to make it easier to use.

Usage:

ask.py [OPTIONS] [PROMPT]

Options:

-h: Show this help message
-g: Set the no generation limit flag
-k: Keep the temporary prompt file

-P args:           Pass through arguments to llama.cpp
-C context:        Set the context for the prompt (not very useful)
-c size:           Set context size prompt (default: 4096)
-t temperature:    Set the temperature (default: 0.3)
-f file:           Input prompt file (can be a glob)
-o file:           Output file (can contain {n}, {m}, {f} for round, model, and file)
-p preset:         Set the preset to use (default: explain_this)
-m model:          Set the model to use. This can be a string in which case the first substring match in ~/Downloads or MODELS_PATH will be used.
-n rounds:         Set the number of rounds to run (default: 1)
-x ignore_prefix:  Set the prefix to ignore in the prompt file (default: #!)
-X extra_prompt:   Set the extra prompt to add to the assistant output (default: "")
-T template:       Set the template to use (default: chatml, but we hardcode some models to use different templates)

"""

# Note that we are using --verbose-prompt by assuming this patch is applied:
VERBOSE_PROMPT_PATCH = """
diff --git a/examples/main/main.cpp b/examples/main/main.cpp
index 374ed47a..36d6cd51 100644
--- a/examples/main/main.cpp
+++ b/examples/main/main.cpp
@@ -496,7 +496,7 @@ int main(int argc, char ** argv) {
     }

     bool is_antiprompt        = false;
-    bool input_echo           = true;
+    bool input_echo           = params.verbose_prompt;
     bool display              = true;
     bool need_to_save_session = !path_session.empty() && n_matching_session_tokens < embd_inp.size();
"""

import datetime
import getopt
import glob
import os
import shutil
import subprocess
import sys
import tempfile

LLAMA_CPP_PATH = os.environ.get("LLAMA_CPP_PATH") or shutil.which('llama-cli') or os.path.expanduser("~/projects/llama.gguf/llama-cli")
MODELS_PATH = os.environ.get("MODELS_PATH") or os.path.expanduser("~/Downloads/")

DEFAULT_MODEL = "gemma-2-9b-it"
# DEFAULT_CODE_GENERATION_MODEL = "SuperNova-Medius"
DEFAULT_CODE_INSTRUCT_MODEL = "Qwen2.5-Coder-32B-Instruct"
# Apparently the instruct models got their <fim> capabilities tuned away. (DeepSeek v2.5 seems fine though)
DEFAULT_CODE_GENERATION_MODEL = "Qwen2.5-Coder-32B-Instruct"

# Presets

class Preset:
    def __init__(self, user_prompt):
        self.user_prompt = user_prompt
        self._system_message = ""

    def prompt(self):
        raise NotImplementedError("Please implement this method in a subclass", self.__class__)

    def system_message(self):
        assert self._system_message is not None
        return self._system_message

    def set_system_message(self, message):
        assert message is not None
        self._system_message = message


    def templated_prompt(self):
        # Implemented by mixins if needed
        return self.prompt()

    def has_postprocess(self):
        return False

    def override_model(self):
        return None

class EmptyPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
    def prompt(self):
        return self.user_prompt
    name = "empty"

class DefaultPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful, thoughtful and creative AI assistant. Give concise answers unless the answer would be better with more detail."

    name = "default"
    def prompt(self):
        return self.user_prompt

class CliPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful AI assistant."

    name = "cli"

    def get_os(self):
        import platform
        override = {'Darwin': 'macOS'}
        ret = platform.system()
        return override.get(ret) or ret

    def prompt(self):
        return f"[Only give the command. Do not explain unless necessary, but if you explain, put it in the form of comments appropriate for the script/language you are using as output. IMPORTANT: Make sure the output is executable.]\n[Environment: {os.environ.get('SHELL')}; Operating System: {self.get_os()}\n\n{self.user_prompt}\n"

class ExplainPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful, thoughtful and creative AI assistant. Give concise answers unless the answer would be better with more detail."

    name = "explain_this"

    def prompt(self):
        if self.context:
            return f"In the context of {self.context}, please explain the following. Be concise in your answer.\n```{self.user_prompt}```\n"
        else:
            return f"Please explain the following.\n```{self.user_prompt}```\n"

class AskUserPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self._system_message = "You are a helpful, thoughtful and creative AI assistant."
        self._user_question = None
        self._override_model = None
    name = "ask_user"

    example_ini = """
[code_review]
question = Review this code.

[fill_in]
question = Fill in the parts marked with "..." according to the comments. Keep the comments except "...".

[whatswrong]
question = Identify errors (if any) with this code and suggest replacements. Just write out the code that needs to be changed,.

[didwemiss]
question = Did we miss anything? Be creative and thoughtful. Do not point out mundane things just for the sake of answering though.
model = gemma-2-9b
"""

    def override_model(self):
        return self._override_model

    def prompt(self):
        import configparser
        if self._user_question is None:
            # Read user-defined preset questions from ~/.config/ask/presets.ini using configparser
            config = configparser.ConfigParser()
            config.read(os.path.expanduser("~/.config/ask/presets.ini"))
            presets = config.sections()
            choices = {}
            models = {}
            for i, preset in enumerate(presets, 1):
                question = config[preset]['question']
                # print(f"{i}. {preset}: {question[:30]}")
                print(f"{i}. \033[1m{preset}\033[0m: {question[:30]}")
                choices[i] = config[preset]['question']
                models[i] = config[preset].get('model') # OK to be None

            cache_dir = os.path.expanduser("~/.cache/ask")
            os.makedirs(cache_dir, exist_ok=True)
            history_file = os.path.join(cache_dir, "history.txt")

            # In addition to presets, show the last 3 entries from ~/.cache/ask/history.txt . These history choices are to be named 'a', 'b', 'c' (to separate them from the numeric ones)
            with open(history_file, "r") as f:
                history = f.readlines()
                for i, entry in enumerate(history[-3:], 1):
                    abc = chr(i + ord('a') - 1)
                    qqq = entry.strip().split('\t', 1)[-1]
                    print(f"{abc}. " + qqq)
                    choices[abc] = qqq

            user_input = input("Choose a preset (or type a custom question): ")
            if user_input.isdigit() and 1 <= int(user_input) <= len(presets):
                self._user_question = choices[int(user_input)]
                self._override_model = models[int(user_input)]
            elif user_input.strip() in choices.keys():
                self._user_question = choices[user_input.strip()]
            else:
                self._user_question = user_input

            # If the user inputs a single number, replace it with the preset value instead
            if self._user_question.isdigit() and 1 <= int(self._user_question) <= len(presets):
                self._user_question = config[presets[int(self._user_question) - 1]]['question']

            # Remember the question in ~/.cache/ask/history.txt
            with open(history_file, "a") as f:
                f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{self._user_question}\n")

        if len(self.user_prompt) < 4096:
            return f"{self._user_question}\n(Please be concise unless the answer requires in-depth analysis)\n--- Start of data ---\n\n{self.user_prompt}\n\n--- End of data ---\n"
        else:
            # For longer contexts, repeat the question/instruction at the end
            return f"{self._user_question}\n(Please be concise unless the answer requires in-depth analysis)\n--- Start of data ---\n\n{self.user_prompt}\n\n--- End of data ---\n\n{self._user_question}\n(Please be concise unless the answer requires in-depth analysis)\n"


class GitCommitSummarizePreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful, thoughtful and creative AI assistant."

    name = "gitcommit"

    def postprocess(self, outs):
        outs = outs.replace('[end of text]', '').rstrip()
        return "\n".join(line.strip() if line.strip() == '' else '🤖 ' + line.rstrip() for line in outs.splitlines(keepends=False))

    def has_postprocess(self):
        return True

    def prompt(self):
        return f"""
Please write a summary of the changes as a git commit message. The first line must be very concise and short. Subsequent paragraph(s) should be concise, but make sure you mention all important and interesting points.

```
{self.user_prompt}
```

Please write a summary of the changes as a git commit message. The first line must be very concise and short. Subsequent paragraph(s) should be concise, but make sure you mention all important and interesting points.
"""


class SummarizePreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful, thoughtful and creative AI assistant."

    name = "summarize"

    def prompt(self):
        return f"""
Please summarize the following text. Be concise (i.e. avoid superfluous writing), but make sure you mention all important and interesting points.

```
{self.user_prompt}
```

Please summarize the above text. Be concise (i.e. avoid superfluous writing), but make sure you mention all important and interesting points.
"""


class ReviewPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
        self.context = context
        self._system_message = "You are a helpful, thoughtful and creative AI assistant. Give concise answers unless the answer would be better with more detail."

    name = "review"

    def prompt(self):
        return f"Please review the following text. Point out (a) mistakes (if any), (b) suggestions for improvements, and (c) other comments that may be relevant. Be thoughtful and creative. Don't just make trivial comments on low hanging fruit. Be engaging. \n```{self.user_prompt}```\n"

class CodeReviewPreset(Preset):
    def __init__(self, user_prompt, context):
        super().__init__(user_prompt)
    name = "code_review"

    def prompt(self):
        return f"Please assume this is a code snippet in a github pull request. Please review the following code. Focus on potential problems. Because it is a snippet, do not be concerned with undefined or unknown references as long as they seem to be reasonable. Be concise in your answer.\n```{self.user_prompt}```\n"

class CodeGenerationPreset(Preset):
    name = "code_generation"
    def __init__(self, file_name, offset):
        super().__init__("")
        self.file_name = file_name
        self.offset = int(offset)

        # assert file size is less than 10kb
        assert os.path.getsize(self.file_name) < 50000
        self.content_bytes = open(self.file_name, "rb").read()

        # while self.content_bytes[self.offset] != ord(b"\n"):
            # self.offset += 1

    def path(self):
        return self.file_name
    def code_language(self):
        # XXX: The CodeGeeX4 model wants the language. We may or may not be
        # using this model, but the code seems generally useful enough.
        GUESSES = {
            ".py": "python",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "cpp",
            ".hpp": "cpp",
            ".java": "java",
            ".js": "javascript",
            ".ts": "typescript",
            ".html": "html",
            ".css": "css",
            ".scss": "scss",
            ".sass": "sass",
            ".less": "less",
            ".php": "php",
            ".sql": "sql",
            ".rb": "ruby",
            ".rs": "rust",
            ".vim": "vimscript",
        }
        for x in range(1, 10):
            suffix = self.file_name[-x:]
            if suffix in GUESSES:
                return GUESSES[suffix]

        # Guess from shebang
        if self.content_bytes.startswith(b"#!"):
            shebang = self.content_bytes[:max(self.offset, 128)].split(b"\n")[0]
            if b"python" in shebang:
                return "python"
            if b"bash" in shebang:
                return "bash"
            if b"/sh" in shebang:
                return "bash"

    def prefix(self):
        return self.content_bytes[:self.offset].decode("utf-8")

    def suffix(self):
        return self.content_bytes[self.offset:].decode("utf-8")

class QwenFimMixin:
    def templated_prompt(self):
        return f"""
<|fim_prefix|>{self.prefix()}<|fim_suffix|>{self.suffix()}<|fim_middle|>
""".strip() + "\n"

    def postprocess(self, outs):
        ends = (' [end of text]', '[end of text]', )
        for end in ends:
            if end in outs:
                return outs.split(end)[0]
        return outs

    def has_postprocess(self):
        return True


class ChatMLTemplateMixin:
    def templated_prompt(self):
        if self.system_message():
            return f"""
<|im_start|>system
{self.system_message()}<|im_end|>
<|im_start|>user
{self.prompt()}<|im_end|>
<|im_start|>assistant
            """.strip() + "\n"
        else:
            return f"""
<|im_start|>user
{self.prompt()}<|im_end|>
<|im_start|>assistant
            """.strip() + "\n"

class QwenTemplateMixin(ChatMLTemplateMixin):
    def system_message(self):
        # https://www.reddit.com/r/LocalLLaMA/comments/1gpwrq1/how_to_use_qwen25coderinstruct_without/
        return "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

class InstructionTemplateMixin:
    def templated_prompt(self):
        return f"""

### Instruction:

{self.prompt()}

### Response:

"""

class CodeGeeX4TemplateMixin:
    def templated_prompt(self):
        return f"""

###PATH:{self.path()}
###LANGUAGE:{self.code_language()}
###MODE:LINE
<|code_suffix|>{self.suffix()}<|code_prefix|>{self.prefix()}<|code_middle|>
""".strip() + "\n"

    def postprocess(self, outs):
        # Remove '<|code_middle|>' (somehow llama.cpp emits this, not sure why)
        PREFIX = "<|code_middle|>\n"
        splitter = outs.split(PREFIX)
        return splitter[-1]

    def has_postprocess(self):
        return True

class LlamaTemplateMixin:
    def templated_prompt(self):
        return f"""<s>[INST] <<SYS>>\n{self.system_message()}\n<</SYS>>\n\n{self.prompt()} [/INST]"""

class Phi3TemplateMixin:
    def templated_prompt(self):
        return f"""<|user|>\n{self.prompt()}<|end|>\n<|assistant|>\n"""


class ZephyrTemplateMixin:
    def templated_prompt(self):
        return f"""<|user|>\n{self.prompt()}</s>\n<|assistant|>\n"""

class Gemma2Mixin:
    def templated_prompt(self):
        return f"""<start_of_turn>user\n{self.prompt()}<end_of_turn>\n<start_of_turn>model\n"""

class MiniCPMTemplateMixin:
    def templated_prompt(self):
        return f"""<用户>{self.prompt()}\n<AI>"""

class DeepSeekV2LiteMixin:
    def templated_prompt(self):
        #   "chat_template": "{% if not add_generation_prompt is defined %}{% set add_generation_prompt = false %}{% endif %}{{ bos_token }}{% for message in messages %}{% if message['role'] == 'user' %}{{ 'User: ' + message['content'] + '\n\n' }}{% elif message['role'] == 'assistant' %}{{ 'Assistant: ' + message['content'] + eos_token }}{% elif message['role'] == 'system' %}{{ message['content'] + '\n\n' }}{% endif %}{% endfor %}{% if add_generation_prompt %}{{ 'Assistant:' }}{% endif %}"
        if self.system_message():
            return f"""{self.system_message()}\n\nUser: {self.prompt()}\n\nAssistant: """
        else:
            return f"""User: {self.prompt()}\n\nAssistant: """

class DeepSeekV25Mixin:
    def templated_prompt(self):
        if self.system_message():
            return f"""<｜begin▁of▁sentence｜>{self.system_message()}<｜User｜>{self.prompt()}<｜Assistant｜>"""
        else:
            return f"""<｜begin▁of▁sentence｜><｜User｜>{self.prompt()}<｜Assistant｜>"""

class MistralLargeInstructTemplate(ChatMLTemplateMixin):
    # "chat_template": "{%- if messages[0]['role'] == 'system' %}\n    {%- set system_message = messages[0]['content'] %}\n    {%- set loop_messages = messages[1:] %}\n{%- else %}\n    {%- set loop_messages = messages %}\n{%- endif %}\n\n{{- bos_token }}\n{%- for message in loop_messages %}\n    {%- if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}\n        {{- raise_exception('After the optional system message, conversation roles must alternate user/assistant/user/assistant/...') }}\n    {%- endif %}\n    {%- if message['role'] == 'user' %}\n        {%- if loop.last and system_message is defined %}\n            {{- '[INST] ' + system_message + '\\n\\n' + message['content'] + '[/INST]' }}\n        {%- else %}\n            {{- '[INST] ' + message['content'] + '[/INST]' }}\n        {%- endif %}\n    {%- elif message['role'] == 'assistant' %}\n        {{- ' ' + message['content'] + eos_token}}\n    {%- else %}\n        {{- raise_exception('Only user and assistant roles are supported, with the exception of an initial optional system message!') }}\n    {%- endif %}\n{%- endfor %}\n",
    def templated_prompt(self):
        # wtf? If we really believe the chat_template above, this is the result. But it doesn't work.
        # return f"""\n    \n\n\n<s>\n\n    \n    \n        \n            [INST] {self.prompt()}[/INST]\n        \n    \n        """
        return f"""<s>[INST] {self.prompt()}[/INST] """

        # Funny enough, the ChatMLTemplateMixin also works...


class Llama3TemplateMixin:
    def templated_prompt(self):
        return f"""
<|begin_of_text|><|start_header_id|>system<|end_header_id|>
{self.system_message()}<|eot_id|><|start_header_id|>user<|end_header_id|>
{self.prompt()}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
""".strip() + "\n"

class WizardLmMixin:
    def templated_prompt(self):
        return f"""
USER: {self.prompt()}
ASSISTANT:
""".strip() + " "

class CommandRPlusTemplateMixin:
    # <BOS_TOKEN><|START_OF_TURN_TOKEN|><|SYSTEM_TOKEN|>{system_prompt}<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|USER_TOKEN|>{prompt}<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|><|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|>

    def templated_prompt(self):
        return f"""<|START_OF_TURN_TOKEN|><|SYSTEM_TOKEN|>{self.system_message()}<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|USER_TOKEN|>{self.prompt()}<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|><|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|>"""


NAME_MATCH_OVERRIDE = [
    ("command-r-plus", CommandRPlusTemplateMixin),
    ("wizardlm", WizardLmMixin),
    ("phi-3-", Phi3TemplateMixin),
    ("zephyr", ZephyrTemplateMixin),
    ("llama-2", LlamaTemplateMixin),
    ("mixtral-8x7b-instruct", LlamaTemplateMixin),
    ("dolphin", ChatMLTemplateMixin),
    ("orange", ChatMLTemplateMixin),
    ("llama-3", Llama3TemplateMixin),
    ("minicpm", MiniCPMTemplateMixin),
    ("DeepSeek-V2-Lite", DeepSeekV2LiteMixin),
    ("DeepSeek-V2.5", DeepSeekV25Mixin),
    ("qwen2", QwenTemplateMixin),
    ("Qwen2", QwenTemplateMixin),
    ("tinyllama_v1.1", ChatMLTemplateMixin),
    ("gemma-2", Gemma2Mixin),
    ("Mistral-Large-Instruct", MistralLargeInstructTemplate),
]

FIM_MATCH_OVERRIDE = [
    ("Qwen2", QwenFimMixin),
    ("codegeex4", CodeGeeX4TemplateMixin),
]


def read_prompt_file(prompt_file, ignore_prefix="#!", system_prefix="SYSTEM:"):
    lines = []
    system = []
    with open(prompt_file, "r") as f:
        while line := f.readline():
            if line.startswith(ignore_prefix):
                if line.startswith(ignore_prefix + system_prefix):
                    system.append(line[len(ignore_prefix) + len(system_prefix):])
                continue
            lines.append(line)
    return {
        "user": "".join(lines),
        "system": "".join(system).strip()
        }


if __name__ == "__main__":
    PRESETS = {}
    # loop through all classes in this file and add them to the presets
    import inspect
    for name, obj in inspect.getmembers(sys.modules[__name__]):
        if inspect.isclass(obj):
            if issubclass(obj, Preset) and obj != Preset:
                PRESETS[obj.name] = obj
    opt_list, args = getopt.getopt(sys.argv[1:], "qhkP:C:c:t:f:o:p:m:n:x:gX:T:v")
    opts = dict(opt_list)

    # Default to explain_this if we don't have a file. If we have a file it's better to assume the file contains a full prompt
    if opts.get("-p") is None:
        preset = ExplainPreset if "explain" in sys.argv[0] else DefaultPreset
    else:
        preset = PRESETS.get(opts.get("-p"))
    assert preset is not None

    user_prompt = None
    prompt_globs = []
    if opts.get("-f"):
        if preset is CodeGenerationPreset:
            user_prompt = opts.get("-f")
        else:
            prompt_globs = sorted(glob.glob(opts.get("-f")))
    elif args:
        user_prompt = " ".join(args)
    else:
        # if it's a terminal print the prompt
        if sys.stdin.isatty():
            print("What is your question?")
        user_prompt = sys.stdin.read()

    template = opts.get("-T") or "chatml"

    if opts.get("-t") is not None:
        temperature = float(opts.get("-t"))
    elif int(opts.get("-n") or 1) == 1 and user_prompt is None:
        # Use 0 if we only run once on a file
        temperature = 0.0
    else:
        # Default to 0.3
        temperature = 0.3

    context = opts.get("-C") or ""
    gguf_context_size = opts.get("-c", "0")
    model_name = opts.get("-m") or DEFAULT_MODEL
    if preset in (AskUserPreset, CodeReviewPreset):
        model_name = DEFAULT_CODE_INSTRUCT_MODEL
    if preset in (CodeGenerationPreset,):
        model_name = DEFAULT_CODE_GENERATION_MODEL
    cmd_args = []
    cmd_args.append("--no-escape")  # llama.cpp just doesn't do sane defaults...
    assert temperature >= 0
    cmd_args.append("--temp")
    cmd_args.append(str(temperature))

    is_mac = "Darwin" in subprocess.run(["uname"], capture_output=True).stdout.decode("utf-8").strip()
    if opts.get("-g") or is_mac:
        cmd_args.append("-ngl")
        cmd_args.append("99")

    if "-q" not in opts:
        # If not quiet mode, verbose prompt
        cmd_args.append("--verbose-prompt")

    templateMixIn = None
    overrideTemplateMixIn = None
    if template == "chatml":
        templateMixIn = ChatMLTemplateMixin
    elif template == "llama":
        templateMixIn = LlamaTemplateMixin
    else:
        templateMixIn = InstructionTemplateMixin

    if preset is CodeGenerationPreset:
        # Force template to be code completion
        model = glob.glob(f"{MODELS_PATH}/*{model_name}*.gguf")[0]
        for model_substring, tm in FIM_MATCH_OVERRIDE:
            if model_substring.lower() in model.lower():
                overrideTemplateMixIn = tm
                break
        else:
            if overrideTemplateMixIn is None:
                print(f"Warning: No template found for {model}, using QwenFimMixin as a fallback")
                overrideTemplateMixIn = QwenFimMixin

        context = args[0]
        # We need a file for code generation
        assert opts.get("-f") is not None

        cmd_args.append("-c")
        cmd_args.append(gguf_context_size)

        cmd_args.append("--n-predict")
        cmd_args.append("200")
    else:
        cmd_args.append("-c")
        cmd_args.append(gguf_context_size)

    # Passthrough arguments
    if opts.get("-P"):
        cmd_args += opts.get("-P").strip().split()

    # If -n or --n-predict is not in the args, we add "--n-predict", "-2"
    if '-n' not in cmd_args and '--n-predict' not in cmd_args:
        cmd_args += ["--n-predict", "-2"] # -2 means fill context


    class ModelPlaceholder:
        pass

    cmd = [LLAMA_CPP_PATH,] + cmd_args + ["-m", ModelPlaceholder]

    assert_count = 0
    for model in glob.glob(f"{MODELS_PATH}/*{model_name}*.gguf") or [model_name]:
        if '-of-000' in model and '01-of-000' not in model:
            # Only use the first shard
            continue
        assert_count += 1
        assert assert_count == 1, "We need to refactor this so that we don't iterate on the model since we actually don't need to"

        if overrideTemplateMixIn is None:
            for model_substring, tm in NAME_MATCH_OVERRIDE:
                if model_substring.lower() in model.lower():
                    overrideTemplateMixIn = tm
                    break
            else:
                if overrideTemplateMixIn is None:
                    print(f"Warning: No template found for {model}, using ChatMLTemplateMixin as a fallback")
                    overrideTemplateMixIn = ChatMLTemplateMixin

        class CurrentPrompt(overrideTemplateMixIn, preset):
            pass
        for prompt_file, prompt in zip([None,] + prompt_globs, [{"user":user_prompt},] + [read_prompt_file(prompt_file, ignore_prefix=opts.get("-x") or "#!") for prompt_file in prompt_globs]):
            if prompt.get("user") is None:
                continue

            if "-v" in opts:
                print(prompt_file)

            # Create a temporary file for storing the prompt
            with tempfile.NamedTemporaryFile(mode="w", delete=('-k' not in opts)) as temp_prompt_file:
                cp = CurrentPrompt(prompt.get("user"), context)
                sys_prompt = prompt.get("system")
                if sys_prompt:
                    cp.set_system_message(sys_prompt)
                templated_prompt = cp.templated_prompt()
                if "-v" in opts:
                    print(templated_prompt)
                temp_prompt_file.write(templated_prompt)
                # Try to fix an apparent llama.cpp bug where it chops off the last newline
                if templated_prompt[-1] == "\n":
                    temp_prompt_file.write("\n")
                if extra := opts.get("-X"):
                    # Extra prompt as prefix of assistant output
                    temp_prompt_file.write(extra)
                    if extra[-1] != "\n":
                        temp_prompt_file.write("\n")
                temp_prompt_file.flush()

                for infer_round in range(int(opts.get("-n") or 1)):
                    out_file = opts.get("-o")
                    if '-m' not in opts: # allow overriding the model if the user did not specify it.
                        if cp.override_model() is not None:
                            try:
                                try_model = glob.glob(f"{MODELS_PATH}/*{cp.override_model()}*.gguf")[0]
                                if not os.path.isfile(try_model):
                                    raise Exception(try_model + " exists but is not a file?!")
                                model = try_model
                            except Exception as e:
                                sys.stderr.write(f"Error using {cp.override_model()} as model: {e}")
                                sys.stderr.write("\n")
                                sys.stderr.flush()
                    if out_file is not None:
                        out_file = (out_file.
                            replace('{n}', str(infer_round)).
                            replace('{m}', os.path.basename(model)).
                            replace('{f}', prompt_file))
                        if os.path.exists(out_file):
                            print(f"Skipping {out_file} as it already exists")
                            continue

                    this_cmd = cmd.copy()
                    if 'codellama-70b' in model: # XXX: Temp hack
                        this_cmd.append("-r")
                        this_cmd.append("EOT: true")
                    if 'yi-34b' or 'starling' in model: # XXX: Temp hack
                        this_cmd.append("-r")
                        this_cmd.append("<|im_end|>")
                    if overrideTemplateMixIn == DeepSeekV2LiteMixin:
                        this_cmd.append("-b")
                        this_cmd.append("256") # https://github.com/ggerganov/llama.cpp/issues/7652#issuecomment-2140568771
                    if overrideTemplateMixIn == DeepSeekV25Mixin:  # We don't have enough RAM for 4096
                        ctx_idx = this_cmd.index("-c")
                        if ctx_idx < 0:
                            this_cmd.append("-c")
                            this_cmd.append("2048")
                        else:
                            this_cmd[ctx_idx + 1] = "2048"

                    this_cmd[this_cmd.index(ModelPlaceholder)] = model
                    this_cmd += ["-f", temp_prompt_file.name]
                    if "-v" in opts:
                        print(this_cmd)
                    p = subprocess.Popen(this_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

                    if '-o' not in opts and not cp.has_postprocess():
                        while dat := p.stdout.read(1):
                            sys.stdout.buffer.write(dat)
                            sys.stdout.flush()

                    outs = p.communicate()

                    # Check exit code
                    if p.returncode != 0:
                        sys.stderr.write("Error: " + outs[1].decode("utf-8", errors="replace"))
                        sys.stderr.write("\n")
                        sys.stderr.flush()
                        sys.exit(1)
                    else:
                        outs_s = outs[0].decode("utf-8", errors="replace")
                        if cp.has_postprocess():
                            outs_s = cp.postprocess(outs_s)

                        if out_file is not None:
                            with open(out_file, "w") as f:
                                f.write(outs_s)
                        else:
                            prompt_user = prompt.get("user")
                            if outs_s.startswith(prompt_user):
                                outs_s = outs_s[len(prompt_user):]
                                # This doesn't work at least in some models because <|im_end|>
                                # seems to be tokenized and stringified back to an empty string.
                                # Instead I just modified the llama.cpp code to just output the
                                # results.

                            print(outs_s)
