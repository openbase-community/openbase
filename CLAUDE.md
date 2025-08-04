When defining a Django model, always include a docstring with the first character being an emoji representing the model.

Prefer using viewsets wherever possible, with custom actions on the viewsets. Inherit from `BaseModelViewSet` or `BaseReadyOnlyModelViewSet` (from `config.viewsets`), which sets the `lookup_field` to `"public_id"`

Don't ever manually return `Response` objects indicating errors; Instead you should raise a DRF `ValidationError`.

If you define a custom viewset action, have it return a `Response` with JSON with a key `"message"` with a generic user-friendly message of what the action did, without interpolating specific values. The same goes for one-off non-viewset POST views that perform an action.

For viewset actions with multiple words in the name, remember to use `url_path` to convert the underscores to dashes, e.g.:

```python
@action(detail=False, methods=["post"], url_path="evaluate-preferences")
def evaluate_preferences(self, request):
```

Do NOT use try/except UNLESS you are implementing genuine fallback behavior (printing/logging doesn't count!). No needlessly swallowing errors! Let them propogate!

Prefer `class MyNamedTuple(NamedTuple)` definitions over dictionaries for return types of internal functions.

When using multi-line triple-quoted strings, place them inline and wrap them with `dedent_strip_format`, which combines the functions with those names, or else simply use `detent_strip` if no interpolation is required. Do not use f-strings for multi-line triple-quoted strings. Example:

```python
def my_func(a, b, c):
    system_prompt = dedent_strip_format(
        """\
        This is a long multi-line triple quoted string.
        We need to interpolate some values:
        {a}
        {b}, {c}
        """,
        a=a,
        b=b,
        c=c
    )
```

Only include function docstrings if you feel it is not clear from the function name what the function does. Only document the return value and arguments in the docstring if you think they are complicated enough to warrant additional explanation.

Avoid defining simple same-file helper functions that would only ever be invoked once per parent function, and have no/minimal control structure of their own. In this case, for readibility it is better to inline and have a longer parent function body.

We use TaskIQ instead of celery. You do NOT have to register tasks in `__init__.py`. Each TaskIQ task must be in its own file in this module. Define tasks as in the following example:

```python
broker = import_module("config.taskiq_config").broker

@broker.task
async def process_document(
    document_pk: int,
) -> None:
    document = await Document.objects.aget(pk=document_pk)

    # Do something with document, e.g. call an LLM
```

Remember to use Django async ORM when defining async tasks like above, leveraging `select_related` to avoid issues while accessing related objects.

When calling any AI client libraries, define the client at the top of the file, e.g.:

```python

broker = import_module("config.taskiq_config").broker

client = AsyncOpenAI()

# ...task definition below
```

Finally, to invoke the task, import it locally to avoid circular dependencies, and call the async `.kiq` method:

```python
# Assuming a sync context:
from haven_api.family.tasks.process_document import process_document
async_to_sync(process_document.kiq)(self.pk)
```

To use structured outputs with OpenAI, follow this example:

```python
from pydantic import BaseModel
from langfuse.openai import OpenAI

client = OpenAI()

class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

completion = client.responses.parse(
    model="gpt-4.1",
    messages=[
        {"role": "system", "content": "Extract the event information."},
        {"role": "user", "content": "Alice and Bob are going to a science fair on Friday."},
    ],
    text_format=CalendarEvent,
)

event = completion.choices[0].message.parsed
print(event.name)
print(event.date)
print(event.participants)
```

Use model `gpt-4.1` unless otherwise instructed.

Don't make docstrings for the pydantic `BaseModel` subclasses you define.

Remember: if using triple quoted strings for the prompts, always use `dedent_strip` or `dedent_strip_format` to fix the spacing.

When defining Django models, never make a stored property where a computed property will suffice. If there are invariants that should be maintained regarding the field values, add assertions or fill in defaults in the model's `save` method, remembering to call the superclass `save`.

If you believe created/updated fields are appropriate for a given model, call them `created_at` and `updated_at`.

Always define a `public_id` field on the model. You can choose whether to make this a slug or else use `config.fields.OpaquePublicIdField` (more common).

When defining DRF model serializers, inherit from `config.serializers.BaseModelSerializer` rather than `rest_framework.serializers.ModelSerializer`. This will automatically define the `"id"` correlating to the `public_id` field on the serialized model. You must still explicitly name `"id"` among `Meta.fields` to include it.