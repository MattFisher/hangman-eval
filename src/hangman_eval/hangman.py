from dataclasses import dataclass
from typing import Any, List, Optional, Union

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
    stderr,
    grouped,
)
from inspect_ai.solver import (
    Generate,
    Solver,
    TaskState,
    basic_agent,
    solver,
    system_message,
)
from inspect_ai.tool import Tool, tool
from inspect_ai.util import store

from hangman_eval.datasets import (
    Language,
    get_words_by_difficulty,
    get_words_by_language,
)

DEFAULT_MAX_GUESSES = 10
DEFAULT_LANGUAGE = Language.ENGLISH


@task
def hangman(
    language: str = DEFAULT_LANGUAGE.value,
    difficulty: Optional[int] = None,
    max_guesses: int = DEFAULT_MAX_GUESSES,
    shuffle: bool = True,
    allow_word_guesses: bool = False,
) -> Task:
    """Evaluate an agent's ability to play Hangman

    Args:
        language: The language to use for the words (default: english)
        difficulty: Specific difficulty level to use (1-5), or None for mixed difficulties
        max_guesses: Maximum number of incorrect guesses allowed
        shuffle: Whether to shuffle the words before playing
        allow_word_guesses: Whether to allow the agent to guess the entire word

    Returns:
        Task configured for the hangman game with specified parameters
    """
    try:
        lang_enum = Language(language.lower())
    except ValueError:
        supported = ", ".join([lang.value for lang in Language])
        raise ValueError(
            f"Language '{language}' not supported. Supported languages: {supported}"
        )

    # Get words based on language and optional difficulty
    if difficulty is not None:
        if difficulty < 1 or difficulty > 5:
            raise ValueError("Difficulty must be between 1 and 5")
        word_entries = get_words_by_difficulty(lang_enum, difficulty)
    else:
        word_entries = get_words_by_language(lang_enum)

    # Create samples
    samples = []
    for entry in word_entries:
        samples.append(
            Sample(
                input=(
                    f"You are playing a game of Hangman in {lang_enum.value}. "
                    f"Try to guess the word one letter at a time. "
                    f"The current state shows '_' for unguessed letters and reveals correctly guessed letters. "
                    f"You lose the game after {max_guesses} incorrect guesses. "
                    f"Make smart guesses based on common letter frequencies and word patterns in {lang_enum.value}."
                ),
                target=[entry.word],
                metadata={
                    "word": entry.word,
                    "max_guesses": max_guesses,
                    "difficulty": entry.difficulty,
                    "language": lang_enum.value,
                    "allow_word_guesses": allow_word_guesses,
                },
            )
        )

    dataset = MemoryDataset(samples)
    if shuffle:
        dataset.shuffle()

    return Task(
        dataset=dataset,
        solver=hangman_player(allow_word_guesses=allow_word_guesses),
        setup=game_initialiser(),
        scorer=game_scorer(),
    )


@dataclass
class GameState:
    word: str
    guessed_letters: list[str]
    remaining_guesses: int
    game_over: bool = False
    won: bool = False

    @staticmethod
    def start(word: str, max_guesses: int = DEFAULT_MAX_GUESSES) -> "GameState":
        return GameState(
            word=word.lower(),
            guessed_letters=[],
            remaining_guesses=max_guesses,
        )

    @property
    def current_state(self) -> str:
        """Returns the current state of the word with unguessed letters as '_'"""
        return " ".join(
            letter if letter in self.guessed_letters else "_" for letter in self.word
        )

    @property
    def incorrect_guesses(self) -> List[str]:
        """Returns list of incorrect guesses"""
        return sorted(list(set(self.guessed_letters) - set(self.word)))

    def guess(self, letter: str) -> "GameState":
        """Process a letter guess and return the new game state"""
        if self.game_over:
            return self

        letter = letter.lower()
        if len(letter) != 1 or not letter.isalpha():
            raise ValueError("Guess must be a single letter")

        if letter in self.guessed_letters:
            return self

        self.guessed_letters.append(letter)

        if letter not in self.word:
            self.remaining_guesses -= 1

        # Check win condition
        if all(letter in self.guessed_letters for letter in self.word):
            self.game_over = True
            self.won = True

        # Check lose condition
        if self.remaining_guesses <= 0:
            self.game_over = True

        return self


@tool(parallel=False)
def hangman_guess() -> Tool:
    """Tool for guessing letters in the hangman game"""

    async def execute(letter: str) -> dict[str, Union[bool, int, str, List[str]]]:
        """Submit a letter guess for the current hangman game.

        Args:
            letter (str): A single letter to guess

        Returns:
            A dictionary containing:
            - current_state: The word with guessed letters revealed and unguessed letters as '_'
            - remaining_guesses: Number of guesses remaining
            - incorrect_guesses: List of incorrect letters guessed
            - game_over: Whether the game has ended
            - won: Whether the player won (only valid if game_over is True)
            - language: The language of the current word
        """
        game_state = store().get("hangman:game_state", None)
        metadata: dict[str, Any] = store().get("hangman:metadata", {})

        if game_state is None:
            raise RuntimeError(
                "No game in progress. The game must be started by the evaluation setup."
            )

        if not game_state.game_over:
            game_state.guess(letter)  # Updates the game state

        return {
            "current_state": game_state.current_state,
            "remaining_guesses": game_state.remaining_guesses,
            "incorrect_guesses": game_state.incorrect_guesses,
            "game_over": game_state.game_over,
            "won": game_state.won,
            "language": metadata.get("language", DEFAULT_LANGUAGE.value),
        }

    return execute


@solver
def hangman_player(allow_word_guesses: bool = False) -> Solver:
    """Solver that uses the hangman_guess tool to play hangman"""
    SYSTEM_MESSAGE = """
    You are playing a game of Hangman. Try to guess the word one letter at a time.
    The current state shows '_' for unguessed letters and reveals correctly guessed letters.
    You have limited guesses before losing. Make smart guesses based on common letter frequencies
    and word patterns in the specified language, and the result of previous guesses.

    Guess one letter at a time using the hangman_guess tool like `hangman_guess("a")`.
    The tool will return:
    - current_state: The word with guessed letters revealed and unguessed letters as '_'
    - remaining_guesses: Number of guesses remaining
    - incorrect_guesses: List of incorrect letters guessed
    - game_over: Whether the game has ended
    - won: Whether the player won (only valid if game_over is True)
    - language: The language of the current word

    Adapt your strategy based on the language.
    """
    system_message_without_word_guesses = (
        SYSTEM_MESSAGE
        + """
    You must guess all the letters in the word to win the game.
    You can ONLY win by guessing all the letters in the word, one at a time.
    Once the game is over, submit("GG") to end the session, whether you have won or lost.
    """
    )
    system_message_with_word_guesses = (
        SYSTEM_MESSAGE
        + """
    You can also guess the entire word at any point before running out of guesses,
    by submitting the word as a single string using `submit("word")`. This will end the game.
    Once the game is over, submit("GG") to end the session, whether you have won or lost.
    """
    )
    final_system_message = (
        system_message_without_word_guesses
        if not allow_word_guesses
        else system_message_with_word_guesses
    )
    return basic_agent(
        init=system_message(final_system_message),
        continue_message="If the game is over, submit('GG') to end the session.",
        tools=[
            hangman_guess(),
        ],
    )


@solver
def game_initialiser() -> Solver:
    """Initialise the game, and store the game state in the store"""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        metadata = state.metadata or {}
        word = metadata.get("word", None)
        if not word:
            raise RuntimeError("No word provided in metadata")

        max_guesses = metadata.get("max_guesses", DEFAULT_MAX_GUESSES)
        language = metadata.get("language", DEFAULT_LANGUAGE.value)
        difficulty = metadata.get("difficulty", 3)
        allow_word_guesses = metadata.get("allow_word_guesses", False)

        hangman_game = GameState.start(
            word=word,
            max_guesses=max_guesses,
        )

        # Store game state and metadata
        state.store.set("hangman:game_state", hangman_game)
        state.store.set(
            "hangman:metadata",
            {
                "language": language,
                "difficulty": difficulty,
                "allow_word_guesses": allow_word_guesses,
            },
        )

        state.user_prompt.text = (
            f"Let's play hangman in {language}! You have {max_guesses} guesses.\n"
            f"The word is {' '.join(['_'] * len(word))}.\n"
        )
        return state

    return solve


@scorer(
    metrics=[
        grouped(accuracy(), group_key="difficulty"),
        stderr(),
    ]
)
def game_scorer() -> Scorer:
    """Score the hangman game based on whether the player won or not"""

    async def score(state: TaskState, target: Target) -> Score:
        game_state = state.store.get("hangman:game_state", None)
        metadata: dict[str, Any] = state.store.get("hangman:metadata", {})
        language = metadata.get("language", DEFAULT_LANGUAGE.value)
        difficulty = metadata.get("difficulty", 3)

        if not game_state:
            raise RuntimeError("No game state found in store")

        allow_word_guesses = metadata.get("allow_word_guesses", False)
        if allow_word_guesses:
            # If word guesses are allowed and the game is not over, the agent guessed early
            if not game_state.game_over:
                guessed_word = state.output.completion
                explanation = (
                    f"Early guess. Word: {game_state.word}. Language: {language}. "
                    f"Difficulty: {difficulty}/5. "
                    f"Guessed word: {guessed_word}. "
                    f"Guessed letters: {game_state.guessed_letters}. "
                    f"Final word state: {game_state.current_state}. "
                    f"Remaining guesses: {game_state.remaining_guesses}. "
                )
                return Score(
                    value=CORRECT if guessed_word == game_state.word else INCORRECT,
                    answer=guessed_word,
                    explanation=explanation,
                )

        if not game_state.game_over:
            return Score(
                value=INCORRECT,
                answer=game_state.current_state,
                explanation="The game did not complete.",
            )

        explanation = (
            f"Game ended. Word: {game_state.word}. Language: {language}. "
            f"Difficulty: {difficulty}/5."
            f"Won: {game_state.won}. "
            f"Guessed letters: {game_state.guessed_letters}. "
            f"Final word state: {game_state.current_state}. "
            f"Remaining guesses: {game_state.remaining_guesses}. "
        )

        return Score(
            value=CORRECT if game_state.won else INCORRECT,
            answer=game_state.current_state,
            explanation=explanation,
        )

    return score
