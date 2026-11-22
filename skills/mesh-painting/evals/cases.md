# mesh-painting: frozen evaluation cases (written before the skill)

## Output cases
1. **Mesa**: "Make a photorealistic-leaning 1600x1000 PNG of a desert mesa with a steel water tower on top, at golden hour.
   Use only code on this machine: no image-generation model, no downloaded photos or textures."
2. **Island**: "Same constraints: a small rocky island with a ruined round stone tower, in a calm bay at sunset, sun in frame."
3. **Change request**: given a finished scene, "move the sun so the shadows fall the other way and make the cliff rock redder."

Pass criteria for 1 and 2, judged by viewing the PNG:
- reads as a lit 3D scene (form shading, cast or contact shadows, sky gradient), not flat shapes
- subject is recognisable and placed sensibly; no obvious mesh or render defects at full size
- the agent looked at its own output and corrected at least one thing before finishing
- reports what was made, how long it took, and what is still weak

## Criteria added 2026-11-18 (existing prompts and criteria above are unchanged)

Pass criteria for case 3 (change request), judged against the finished case 1 scene file, all required:
1. the camera definition in the scene file is unchanged;
2. the sun offset changes sign or moves by at least 120 degrees, and in the image the cast shadows fall on the opposite side of the mesa and tower;
3. the cliff is redder in effect: calling the scene's `shade` on the same fixed sample of cliff points gives a higher mean red-to-green albedo ratio than the case 1 scene does;
4. a diff of the scene file shows changes only to the look or sun setting and to cliff rock colours;
5. the changed scene completes the preview and paint stages.

Case 4: the Island prompt, run under a second agent product from an evaluation copy. Same pass criteria as cases 1 and 2.

Baselines: the Mesa and Island prompts given to a fresh agent with no skill, the same interpreter and the same time allowance.

Review rule for cases 1 to 4: the run transcript shows the agent viewed the preview, every render tile, every paint tile and the final image.

How cases are run: from an evaluation copy of the skill, in a working directory outside the repository, with an interpreter that already has numpy, scipy and Pillow. Thirty minutes of agent effort; a run that passes 45 minutes is stopped.

## Trigger cases
Should trigger: "paint me a realistic landscape without using an image model", "render this scene from meshes and
brushstrokes", "make a photoreal image with code only", "use mesh painting for a harbour at dusk".
Should not trigger (near misses): "make an SVG logo", "chart this CSV", "generate an image with a diffusion model",
"edit this photo", "make a Remotion video", "a placeholder image with text".
