import solution_user_agent  # noqa: F401 - registers the AWS Solutions user-agent hook; import first
import json
import base64
from io import BytesIO
from typing import Optional
from pathlib import Path

from pygments import highlight
from pygments.lexers import PythonLexer
from pygments.formatters import TerminalTrueColorFormatter
import matplotlib.pyplot as plt
from PIL import Image
from print_color import print as print_color

from evaluation import TESTS_JSON, CREDENTIALS_JSON, TESTS_IMG, FAILED_THRESHOLD


UI_TOOLS = {'visualize_image', 'visualize_map_raster_layer'}


def print_msg_type(msg_type: str, end: str = "\n"):
    print_color(f"\n[{msg_type}]", color="purple", end=end)


class CLI_Handler:
    def __init__(self, interactive=False) -> None:
        self.history = []
        self.show_images = interactive
        self.test_id = None

        self.response_text = None
        self.agent_metrics = None
    
    def set_test_id(self, test_id: str):
        self.test_id = test_id

    def handle_user_message(self, message: str):
        self.history.append(("user", message))

    def handle_text_message(self, message: str):
        self.history.append(("assistant", message))
        print_msg_type("Message", end=" ")
        print_color(message, color="green")

    def handle_tool_use(self, name: str, input: dict, image_str: Optional[str] = None):
        if name == 'python_repl':
            code = input['code']
            self.history.append(("assistant", f"python_repl Tool:\n{code}"))
            print_msg_type("Code")
            print(highlight(code, PythonLexer(), TerminalTrueColorFormatter()))

        elif name in UI_TOOLS and image_str:
            image = Image.open(BytesIO(base64.b64decode(image_str)))
            if self.show_images:
                plt.imshow(image)
                plt.axis('off')
                plt.tight_layout()
                plt.show()
            else:
                # Save image in the TESTS_IMG folder
                filename = Path(input['image_path']).name
                if self.test_id:
                    filename = f"{self.test_id.replace('.', '-')}_{filename}"
                image.save(TESTS_IMG / filename, 'PNG')

    def handle_tool_result(self, name: str, output: list[str]):
        for text in output:
            self.history.append(("assistant", f"Tool Output:\n{text}"))
            print_msg_type("Output", end=" ")
            print_color(text, color="yellow")
    
    def handle_result(self, result: dict):
        # The response contains the latest text message, already printed earlier.
        self.response_text = result['response']
        self.agent_metrics = result['metrics']['agent']

        print_msg_type("Metrics")
        print_color(f"- Cost: ${self.agent_metrics.get('on_demand_cost', 0):.2f}", color="green")
        print_color(f"- Cycles: {self.agent_metrics.get('total_cycles', 0)}", color="green")


if __name__ == '__main__':
    tests = {test['id']: test for test in json.load(open(TESTS_JSON))}

    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('test_id', choices=tests.keys())
    parser.add_argument('--interactive', action='store_true') 
    args = parser.parse_args()

    # Test
    test = tests[args.test_id]
    print(f"# Test ID: {args.test_id}")
    print(f"- Question: {test['question']}")
    print(f"- AOI: {test['aoi']}")

    # Agent
    from agent_client import AgentClient
    credentials = json.loads(open(CREDENTIALS_JSON).read())
    agent = AgentClient(credentials['username'],credentials['password'])
    cli_handler = CLI_Handler(args.interactive)
    cli_handler.set_test_id(args.test_id)
    agent.handle_agent_messages(cli_handler, test['question'], test['aoi'])

    # Evaluation
    from llm_as_a_judge import Agent_Evaluator
    evaluator = Agent_Evaluator()
    eval_response = evaluator.evaluate(test['question'], test['answer'], cli_handler.response_text)
    color = "green" if eval_response['score'] > FAILED_THRESHOLD else "red"
    
    print_msg_type("Evaluation")
    print_color(f" - Score: {eval_response['score']:.0%}", color=color)
    print_color(f" - Rationale: {eval_response['rationale']}", color=color)

    if args.interactive:
        while True:
            user_input = input("\n> ")
            if user_input.lower() in ['quit', 'exit']:
                break
            agent.handle_agent_messages(cli_handler, user_input, test['aoi'])
