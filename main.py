import os
from collections import defaultdict

from cg.api import (
    AreaType,
    CardType,
    EnergyType,
    LogType,
    Observation,
    SelectContext,
    OptionType,
    Card,
    Pokemon,
    all_card_data,
    to_observation_class,
)
from ptcg_policy import (
    ATTACK_AURA_JAB,
    ATTACK_COSMIC_BEAM,
    ATTACK_MEGA_BRAVE,
    ATTACK_WILD_PRESS,
    AttackContext,
    AttackPlan,
    PolicyState,
    can_plan_active_attack,
    evaluate_attack,
)

"""
Mega Lucario ex Deck
Intermediate Level
This deck battles by strategically switching between Mega Lucario ex as the main attacker, and Hariyama and Solrock as secondary attackers.
"""

# Load deck.csv in the dataset
file_path = "deck.csv"
if not os.path.exists(file_path):
    file_path = "/kaggle_simulations/agent/" + file_path
with open(file_path, "r") as file:
    csv = file.read().split("\n")
my_deck = []
for i in range(60):
    my_deck.append(int(csv[i]))

# Fetch card metadata database and create an ID-to-Card lookup table
all_card = all_card_data()
card_table = {c.cardId: c for c in all_card}

# Decklist
Makuhita = 673  # ×2
Hariyama = 674  # ×2
Lunatone = 675  # ×2
Solrock = 676  # ×3
Riolu = 677  # ×3
Mega_Lucario_ex = 678  # ×4
Dusk_Ball = 1102  # ×4
Switch = 1123  # ×2
Premium_Power_Pro = 1141  # ×4
Fighting_Gong = 1142  # ×4
Poke_Pad = 1152  # x4
Hero_Cape = 1159  # ×1
Boss_Orders = 1182  # ×2
Carmine = 1192  # ×4
Lillie_Determination = 1227  # ×4
Gravity_Mountain = 1252  # ×2
Basic_Fighting_Energy = 6  # ×13


policy_state = PolicyState()


def reset_for_match() -> None:
    """Reset state when the arena loads this module for a new match."""

    policy_state.reset_match()


def get_card(
    obs: Observation,
    area: AreaType | None,
    index: int | None,
    player_index: int | None,
) -> Pokemon | Card | None:
    """Helper function to safely extract a Card or Pokemon object from specific zones."""
    state = obs.current
    if state is None or area is None or index is None or player_index is None:
        return None
    if not 0 <= player_index < len(state.players):
        return None

    ps = state.players[player_index]
    try:
        match area:
            case AreaType.DECK:
                if obs.select is None or obs.select.deck is None:
                    return None
                return obs.select.deck[index]
            case AreaType.HAND:
                if ps.hand is None:
                    return None
                return ps.hand[index]
            case AreaType.DISCARD:
                return ps.discard[index]
            case AreaType.ACTIVE:
                return ps.active[index]
            case AreaType.BENCH:
                return ps.bench[index]
            case AreaType.PRIZE:
                return ps.prize[index]
            case AreaType.STADIUM:
                return state.stadium[index]
            case AreaType.LOOKING:
                if state.looking is None:
                    return None
                return state.looking[index]
            case _:
                return None
    except IndexError:
        return None


def prize_count(pokemon: Pokemon) -> int:
    """Calculates how many Prize cards a Pokémon yields upon being Knocked Out, factoring in modifiers."""
    data = card_table[pokemon.id]
    count = 3 if data.megaEx else 2 if data.ex else 1
    for card in pokemon.energyCards:
        if card.id == 12:  # Legacy Energy
            count -= 1
    for card in pokemon.tools:
        if card.id == 1172 and "Lillie" in data.name:  # Lillie’s Pearl
            count -= 1
    return max(0, count)


def pokemon_score(pokemon: Pokemon) -> int:
    """Heuristically evaluates the tactical worth of targeting a specific Pokémon on the opponent's field."""
    data = card_table[pokemon.id]
    score = prize_count(pokemon) * 1000
    score += len(pokemon.energies) * 150
    score += len(pokemon.tools) * 100
    if data.stage2:
        score += 250
    elif data.stage1:
        score += 130

    id = pokemon.id
    # Noctowl, Fan Rotom, Archaludon ex, Meowth ex
    if id == 173 or id == 174 or id == 190 or id == 1071:
        score -= 200
    if id == 112 and len(pokemon.energies) >= 1:  # Munkidori
        score += 300
    score += pokemon.hp
    return score


def agent(obs_dict: dict) -> list[int]:
    """Main Agent Function.

    Each element in the returned list must be >= 0 and < len(obs.select.option).
    The list length must be between obs.select.minCount and obs.select.maxCount (inclusive), with no duplicate elements.

    Returns:
        list[int]: A list of option index.
    """
    obs = to_observation_class(obs_dict)
    if obs.select is None:
        # In the initial selection, the obs.select is None, and it is necessary to return the deck.
        # The deck is a list of 60 card IDs.
        # The deck must comply with the Pokémon Trading Card Game rules.
        policy_state.reset_match()
        return my_deck

    state = obs.current
    if state is None:
        raise ValueError("A non-initial selection must include the current state.")
    select = obs.select
    context = select.context
    my_index = state.yourIndex
    my_state = state.players[my_index]
    op_state = state.players[1 - my_index]
    my_prize = len(my_state.prize)

    policy_state.begin_turn(state.turn)
    for log in obs.logs:
        if (
            log.type == LogType.SWITCH
            and log.playerIndex == my_index
            and log.serialActive == policy_state.mega_brave_locked_serial
        ):
            # Moving to the Bench clears Mega Brave's attack lock.
            policy_state.clear_mega_brave_lock()
    plan = policy_state.plan

    field_counts = defaultdict(
        int
    )  # Number of cards per card ID on the Bench and in the Active Spot
    hand_counts = defaultdict(int)  # Number of cards per card ID in hand
    discard_counts = defaultdict(int)  # Number of cards per card ID in discard pile

    attacker1 = False
    attacker2 = False
    for card in my_state.active + my_state.bench:
        if card is None:
            continue
        field_counts[card.id] += 1
        if card.id == Makuhita or card.id == Hariyama:
            if len(card.energies) >= 3:
                attacker2 = True
        elif card.id == Riolu or card.id == Mega_Lucario_ex:
            if len(card.energies) >= 2:
                attacker1 = True

    for card in my_state.hand or []:
        hand_counts[card.id] += 1

    for card in my_state.discard:
        discard_counts[card.id] += 1

    stadium_id = 0
    for card in state.stadium:
        stadium_id = card.id

    if context == SelectContext.MAIN:
        can_switch = False
        can_op_switch = False
        active_attack_ids = set()
        hariyama_evolve_targets = set()
        for option in select.option:
            if option.type == OptionType.PLAY and option.index is not None:
                card = get_card(obs, AreaType.HAND, option.index, my_index)
                if card is None:
                    continue
                if card.id == Switch:
                    can_switch = True
                elif card.id == Boss_Orders:
                    can_op_switch = True
            elif option.type == OptionType.EVOLVE and option.index is not None:
                card = get_card(obs, AreaType.HAND, option.index, my_index)
                if card is None or card.id != Hariyama:
                    continue
                can_op_switch = True
                if option.inPlayIndex is None:
                    continue
                target_index = option.inPlayIndex
                if option.inPlayArea == AreaType.BENCH:
                    target_index += 1
                hariyama_evolve_targets.add(target_index)
            elif option.type == OptionType.RETREAT:
                can_switch = True
            elif option.type == OptionType.ATTACK and option.attackId is not None:
                active_attack_ids.add(option.attackId)

        my_cards = [*my_state.active, *my_state.bench]
        op_cards = [*op_state.active, *op_state.bench]
        best_plan = AttackPlan()

        if state.turn >= 2:
            best_score = -1.0
            for attacker_index, my_pokemon in enumerate(my_cards):
                if my_pokemon is None:
                    continue
                if attacker_index != 0 and not can_switch:
                    break

                attack_ids: tuple[int, ...] = ()
                setup_score = 0
                if my_pokemon.id == Mega_Lucario_ex:
                    attack_ids = (ATTACK_AURA_JAB, ATTACK_MEGA_BRAVE)
                    if my_prize in (2, 3):
                        setup_score -= 500
                elif my_pokemon.id == Hariyama:
                    attack_ids = (ATTACK_WILD_PRESS,)
                elif (
                    my_pokemon.id == Makuhita
                    and attacker_index in hariyama_evolve_targets
                ):
                    attack_ids = (ATTACK_WILD_PRESS,)
                    setup_score -= 100
                elif my_pokemon.id == Solrock:
                    attack_ids = (ATTACK_COSMIC_BEAM,)

                if attacker_index == 0:
                    bench_after_switch = my_cards[1:]
                else:
                    bench_after_switch = [
                        my_cards[0],
                        *my_cards[1:attacker_index],
                        *my_cards[attacker_index + 1 :],
                    ]
                has_lunatone_on_bench = any(
                    pokemon is not None and pokemon.id == Lunatone
                    for pokemon in bench_after_switch
                )

                for attack_id in attack_ids:
                    preview = evaluate_attack(
                        AttackContext(
                            attack_id=attack_id,
                            has_lunatone_on_bench=has_lunatone_on_bench,
                        )
                    )
                    if preview is None:
                        continue

                    more_energy = False
                    energy_count = len(my_pokemon.energies)
                    mega_brave_disabled = (
                        attack_id == ATTACK_MEGA_BRAVE
                        and attacker_index == 0
                        and policy_state.mega_brave_disabled(
                            state.turn, my_pokemon.serial
                        )
                    )
                    if attacker_index == 0 and not can_plan_active_attack(
                        attack_id=attack_id,
                        energy_count=energy_count,
                        energy_required=preview.spec.energy_required,
                        legal_attack_ids=active_attack_ids,
                        attack_blocked=(
                            mega_brave_disabled or my_state.asleep or my_state.paralyzed
                        ),
                    ):
                        continue
                    if energy_count < preview.spec.energy_required:
                        if (
                            hand_counts[Basic_Fighting_Energy] >= 1
                            and not state.energyAttached
                        ):
                            energy_count += 1
                            if energy_count < preview.spec.energy_required:
                                continue
                            more_energy = True
                        else:
                            continue

                    for target_index, op_pokemon in enumerate(op_cards):
                        if op_pokemon is None:
                            continue
                        if target_index != 0 and not can_op_switch:
                            break

                        target_data = card_table[op_pokemon.id]
                        attack_context = AttackContext(
                            attack_id=attack_id,
                            attacker_is_fighting=(
                                card_table[my_pokemon.id].energyType
                                == EnergyType.FIGHTING
                            ),
                            target_is_active=True,
                            target_weak_to_fighting=(
                                target_data.weakness == EnergyType.FIGHTING
                            ),
                            target_resists_fighting=(
                                target_data.resistance == EnergyType.FIGHTING
                            ),
                            premium_power_pro_active=(
                                policy_state.premium_power_pro_active
                            ),
                            has_lunatone_on_bench=has_lunatone_on_bench,
                            discard_basic_fighting=discard_counts[
                                Basic_Fighting_Energy
                            ],
                            bench_target_count=len(my_state.bench),
                            attacker_hp=my_pokemon.hp,
                        )
                        try:
                            evaluation = evaluate_attack(attack_context)
                        except ValueError:
                            continue
                        if evaluation is None or evaluation.damage <= 0:
                            continue

                        premium_evaluation = evaluation
                        if not policy_state.premium_power_pro_active:
                            premium_evaluation = evaluate_attack(
                                AttackContext(
                                    attack_id=attack_id,
                                    attacker_is_fighting=(
                                        card_table[my_pokemon.id].energyType
                                        == EnergyType.FIGHTING
                                    ),
                                    target_is_active=True,
                                    target_weak_to_fighting=(
                                        target_data.weakness == EnergyType.FIGHTING
                                    ),
                                    target_resists_fighting=(
                                        target_data.resistance == EnergyType.FIGHTING
                                    ),
                                    premium_power_pro_active=True,
                                    has_lunatone_on_bench=has_lunatone_on_bench,
                                    discard_basic_fighting=discard_counts[
                                        Basic_Fighting_Energy
                                    ],
                                    bench_target_count=len(my_state.bench),
                                    attacker_hp=my_pokemon.hp,
                                )
                            )

                        prize = 0
                        score = float(pokemon_score(op_pokemon))
                        if op_pokemon.hp <= evaluation.damage:
                            prize = prize_count(op_pokemon)
                        else:
                            score *= evaluation.damage / op_pokemon.hp
                        score += setup_score
                        score += evaluation.aura_attach_count * 60
                        score -= evaluation.self_damage * 2
                        if evaluation.self_knockout:
                            score -= prize_count(my_pokemon) * 1000

                        if len(op_state.prize) <= prize:
                            score = 50000

                        if attacker_index == 0:
                            score += 220
                        if target_index == 0:
                            score += 300
                        score += energy_count

                        if best_score < score:
                            best_score = score
                            premium_damage = (
                                premium_evaluation.damage
                                if premium_evaluation is not None
                                else evaluation.damage
                            )
                            best_plan = AttackPlan(
                                attacker=attacker_index,
                                target=target_index,
                                attack_id=attack_id,
                                remain_hp=op_pokemon.hp - evaluation.damage,
                                energy=more_energy,
                                damage=evaluation.damage,
                                self_damage=evaluation.self_damage,
                                aura_attach_count=evaluation.aura_attach_count,
                                premium_damage_gain=max(
                                    0,
                                    premium_damage - evaluation.damage,
                                ),
                                premium_enables_knockout=(
                                    evaluation.damage < op_pokemon.hp <= premium_damage
                                ),
                            )

        policy_state.plan = best_plan
        plan = policy_state.plan

    # Attach energy score
    def energy_score(pokemon: Pokemon, active: bool) -> int:
        energy_count = len(pokemon.energies)
        score = 8000
        if active:
            score += 10
        if pokemon.id == Makuhita or pokemon.id == Hariyama:
            if pokemon.id == Hariyama:
                score += 1
            if energy_count < 3:
                score += 100
            if attacker2:
                score -= 50
        elif pokemon.id == Lunatone:
            score -= 100
        elif pokemon.id == Solrock:
            if energy_count < 1:
                score += 20
            else:
                score -= 100
        elif pokemon.id == Riolu or pokemon.id == Mega_Lucario_ex:
            if pokemon.id == Mega_Lucario_ex:
                score += 1
            if energy_count < 2:
                score += 100
            if attacker1:
                score -= 50
        return score

    # Iterate over every possible option and assign a heuristic score.
    scores = []  # Score for each action
    for o in select.option:
        score = 0  # The default and baseline score is 0.
        if o.type == OptionType.NUMBER:
            score = o.number  # e.g., for "draw X cards"
        elif o.type == OptionType.YES:
            score = 1  # Prefer "Yes"
        elif o.type == OptionType.CARD:
            card = get_card(obs, o.area, o.index, o.playerIndex)
            if card is not None:
                energy_count = 0
                if isinstance(card, Pokemon):
                    energy_count = len(card.energies)
                if (
                    context == SelectContext.SWITCH
                    or context == SelectContext.TO_ACTIVE
                ):
                    # Selection of the Pokémon to send to the Active Spot
                    if o.playerIndex == my_index:
                        score += energy_count * 2
                        if o.index == plan.attacker - 1:
                            score += 100
                        if card.id == Mega_Lucario_ex:
                            if my_prize == 2 or my_prize == 3:
                                score += 8
                            else:
                                score += 20
                        elif card.id == Hariyama and energy_count >= 2:
                            score += 15
                        elif card.id == Makuhita and energy_count >= 2:
                            score += 10
                        elif card.id == Solrock:
                            score += 5
                        elif card.id == Riolu:
                            score += 4
                    else:
                        if o.index == plan.target - 1:
                            score += 100
                elif context == SelectContext.SETUP_ACTIVE_POKEMON:
                    # Prioritize playing Riolu if going first, and Solrock if going second.
                    if card.id == Solrock:
                        if state.firstPlayer == my_index:
                            score = 2
                        else:
                            score = 4
                    elif card.id == Riolu:
                        score = 3
                    elif card.id == Makuhita:
                        score = 1
                elif context == SelectContext.TO_HAND:
                    score = 200 - hand_counts[card.id] * 100
                    if card.id == Makuhita:
                        if field_counts[card.id] >= 1:
                            score -= 10
                        else:
                            score += 10
                    elif card.id == Hariyama:
                        if field_counts[Makuhita] >= 1:
                            score += 20
                        else:
                            score -= 20
                    elif card.id == Lunatone:
                        if field_counts[card.id] >= 1:
                            score -= 250
                        else:
                            score += 60
                    elif card.id == Solrock:
                        if field_counts[card.id] >= 1:
                            score -= 250
                        else:
                            score += 50
                    elif card.id == Riolu:
                        if field_counts[card.id] + field_counts[Mega_Lucario_ex] >= 2:
                            score -= 150
                        elif field_counts[card.id] + field_counts[Mega_Lucario_ex] >= 1:
                            score -= 3
                        else:
                            score += 40
                    elif card.id == Mega_Lucario_ex:
                        if field_counts[Riolu] >= 1:
                            score += 40
                        else:
                            score -= 15
                    elif card.id == Basic_Fighting_Energy:
                        if not policy_state.ability_used or not state.energyAttached:
                            score += 30
                        else:
                            score -= 1
                elif context == SelectContext.ATTACH_FROM:
                    score = energy_score(card, o.area == AreaType.ACTIVE)
        elif o.type == OptionType.PLAY:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            data = card_table[card.id]
            if data.cardType == CardType.POKEMON:
                score = 20000
                if card.id == Lunatone or card.id == Solrock:
                    if field_counts[card.id] >= 1:
                        score = -1
                elif card.id == Riolu:
                    if field_counts[card.id] + field_counts[Mega_Lucario_ex] >= 2:
                        score = -1
            else:
                score = 10000
                if card.id == Switch:
                    if plan.attacker <= 0:
                        score = -1
                    else:
                        score = 6000
                elif card.id == Premium_Power_Pro:
                    if (
                        plan.attacker < 0
                        or plan.premium_damage_gain <= 0
                        or plan.remain_hp <= 0
                    ):
                        score = -1
                    elif plan.premium_enables_knockout:
                        score = 6500
                    else:
                        score = 5000
                elif card.id == Boss_Orders:
                    if plan.target >= 1:
                        score = 3200
                    else:
                        score = -1
                elif card.id == Carmine:
                    score = 3000
                elif card.id == Lillie_Determination:
                    score = 3100
                elif card.id == Gravity_Mountain:
                    if stadium_id == 0:
                        score = -1
        elif o.type == OptionType.ATTACH:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            if card.id == Hero_Cape:
                score = 7000
                if pokemon.id == Riolu:
                    score += 100
                elif pokemon.id == Mega_Lucario_ex:
                    score += 200
            else:
                score = energy_score(pokemon, o.inPlayArea == AreaType.ACTIVE)
                if o.inPlayArea == AreaType.ACTIVE:
                    if plan.attacker == 0 and plan.energy:
                        score += 200
                else:
                    if plan.attacker == 1 + o.inPlayIndex and plan.energy:
                        score += 200
        elif o.type == OptionType.EVOLVE:
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            score = 9000 + len(pokemon.energies)
            if pokemon.id == Makuhita and plan.target == 0:
                score = -1
        elif o.type == OptionType.ABILITY:
            card = get_card(obs, o.area, o.index, my_index)
            if card.id == 1267:  # Lumiose City
                score = 1
            else:
                score = 30000
        elif o.type == OptionType.RETREAT:
            if plan.attacker >= 1:
                score = 2000
            else:
                score = -1
        elif o.type == OptionType.ATTACK:
            score = 1000
            if o.attackId == plan.attack_id:
                score += 100

        scores.append(score)

    # Select in descending order of score
    desc_indices = [
        i for i, _ in sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    ]
    if context == SelectContext.MAIN:
        o = select.option[desc_indices[0]]
        if o.type == OptionType.ABILITY:
            card = get_card(obs, o.area, o.index, my_index)
            if card is not None and card.id == Lunatone:
                policy_state.ability_used = True
        elif o.type == OptionType.PLAY:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            if card is not None and card.id == Premium_Power_Pro:
                policy_state.premium_power_pro_active = True
        elif o.type == OptionType.ATTACK and o.attackId == ATTACK_MEGA_BRAVE:
            active = my_state.active[0] if my_state.active else None
            if active is not None:
                policy_state.note_mega_brave(state.turn, active.serial)
    return desc_indices[: select.maxCount]
