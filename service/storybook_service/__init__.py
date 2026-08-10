"""HTTP service that runs the `story-book` CLI on behalf of the iOS client.

The pipeline is unchanged and unwrapped: this package shells out to the same CLI a laptop user
runs. Nothing here reimplements a stage, and nothing here writes to `src/story_book/`.
"""

__version__ = "0.1.0"
