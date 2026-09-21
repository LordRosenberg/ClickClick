# Task examples

[README](../README.md) · [中文](task-examples.zh-CN.md) · [Demo videos](demos.md) · [Deployment](deployment.md)

Describe what you want done in your own words. ClickClick plans the steps and acts on the current screen. These examples illustrate possible tasks, not a required prompt template; adapt their wording, apps and data freely.

## Cross-app transfer

For example, simply ask:

> Add the recipes in the open Markor note to Broccoli.

Add any constraints that matter to you, such as preserving the source text or keeping existing recipes unchanged. The agent handles reading, app switching and form entry. [Watch a three-recipe run](assets/demos/notes-to-recipes.mp4).

## Extract structured data from an image

> Read the expenses from expenses.jpg in Simple Gallery Pro and add them to Pro Expense, including each name, amount, category and note.

The agent reads the source image, carries its contents into the destination app and checks the created records. [Watch the recorded run](assets/demos/image-to-expenses.mp4).

## Configure a recurring event

For example: “Add a morning meeting tomorrow at 9 am, lasting 30 minutes and repeating every weekday.” The agent translates the request into the app’s date, time and recurrence settings. [Watch an event configured end to end](assets/demos/recurring-calendar-event.mp4).

## Verify the result

For creation tasks, check the number of new records, each required field and preservation of existing records. For deletion tasks, check both the requested removals and remaining records. Console provides the action history, screenshots and input receipts; application state establishes what was saved.

<a id="metric-definitions"></a>
### Record-level metrics

A required record created with all specified fields correct, or a requested deletion, is a true positive. An extra or incorrect result is a false positive; a missing required result is a false negative. Precision is TP/(TP+FP), and recall is TP/(TP+FN). Track unintended changes to other records separately. The [controlled task study](runtime-study.md) demonstrates field and preservation audits.

<a id="try-a-multi-record-workflow"></a>
## Optional: check multi-record entry with fixed data

The following example supplies two fixed recipes so you can check each saved field and compare configurations. The field list is example input data, not a required ClickClick prompt format.

1. Finish the [README device initialization](../README.md#deployment). Install Broccoli using a distribution linked by its [official project](https://github.com/flauschtrud/broccoli). Launch it manually and finish onboarding. For cross-app mode, also install [Markor](https://github.com/gsantner/markor), finish onboarding and grant its requested file access.
2. Use a test collection. Confirm the two titles below do not already exist; for another run, give both titles a new common suffix and use it consistently. Record the original recipe count before submitting.
3. For direct entry, paste the following instruction into Console, select one device and the two role models, then click **submit**.

```text
In Broccoli, create exactly these two recipes. Preserve all other recipes.
Keep the supplied text unchanged. Save each recipe and check its saved fields.

Recipe 1
Title: ClickClick Demo Oats
Description: A small breakfast test recipe.
Servings: 1
Preparation time: 10 minutes
Source: https://example.com/clickclick-demo
Ingredients:
40 g oats
200 ml water
Directions:
Bring the water to a boil. Add the oats and simmer for 5 minutes.
Favorite: no

Recipe 2
Title: ClickClick Demo Salad
Description: A small lunch test recipe.
Servings: 2
Preparation time: 15 minutes
Source: https://example.com/clickclick-demo
Ingredients:
1 cucumber
2 tomatoes
Directions:
Wash and chop the vegetables. Mix them in a bowl.
Favorite: yes
```

4. For cross-app mode, first save the two recipe blocks as `clickclick-demo.md` in Markor and leave that note open. Submit: **Read the two recipes in the currently open Markor note clickclick-demo.md, then create exactly those two recipes in Broccoli. Preserve their text and all existing recipes; save and check the new records.** This exercises source collection and app switching instead of providing the field values directly to the task instruction.
5. In Console, inspect stage transitions, active skills and the actual model inputs. Confirm source information remains available after switching apps; inspect input receipts where present. In Broccoli, check that the count increased by exactly two, all eight fields match and original entries are unchanged. Record task elapsed time and model usage from the task view, retaining unavailable usage as unknown.

To compare configurations, restore the same collection and source note, keep app versions and task text fixed, and retain every attempt. Benchmark setup is covered separately in the [evaluation guide](evaluation.md).
