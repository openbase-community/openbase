from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CartesiaVoiceCatalogEntry:
    id: str
    name: str
    language: str
    country: str | None
    gender: str | None


CARTESIA_VOICE_CATALOG: tuple[CartesiaVoiceCatalogEntry, ...] = (
    CartesiaVoiceCatalogEntry(
        "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc", "Jacqueline", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", "Skylar", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "e07c00bc-4134-4eae-9ea4-1a55fb45746b", "Brooke", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "630ed21c-2c5c-41cf-9d82-10a7fd668370", "Corey", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "62ae83ad-4f6a-430b-af41-a9bede9286ca", "Gemma", "en", "GB", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "ef191366-f52f-447a-a398-ed8c0f2943a1", "Archie", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "47c38ca4-5f35-497b-b1a3-415245fb35e1", "Daniel", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "f786b574-daa5-4673-aa0c-cbe3e8534c02", "Katie", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "5ee9feff-1265-424a-9d7f-8e4d431a12c7", "Ronald", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "f9836c6e-a0bd-460e-9d3c-f7299fa60f94", "Caroline", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "a167e0f3-df7e-4d52-a9c3-f949145efdab", "Blake", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "e8e5fffb-252c-436d-b842-8879b84445b6", "Cathy", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e", "Theo", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "2f251ac3-89a9-4a77-a452-704b474ccd01", "Lucy", "en", "GB", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "f6ff7c0c-e396-40a9-a70b-f7607edb6937", "Emma", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "a33f7a4c-100f-41cf-a1fd-5822e8fc253f", "Lauren", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "a5136bf9-224c-4d76-b823-52bd5efcffcc", "Jameson", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "f039066f-cdb7-45ed-b51d-1034ae2f04a0", "Cindy", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "ee7ea9f8-c0c1-498c-9279-764d6b56d189", "Oliver", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "86e30c1d-714b-4074-a1f2-1cb6b552fb49", "Carson", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991", "George", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "87286a8d-7ea7-4235-a41a-dd9fa6630feb", "Henry", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "4f7f1324-1853-48a6-b294-4e78e8036a83", "Casper", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "c8f7835e-28a3-4f0c-80d7-c1302ac62aae", "Alistair", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "dc30854e-e398-4579-9dc8-16f6cb2c19b9", "Victoria", "en", "GB", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "0ad65e7f-006c-47cf-bd31-52279d487913", "Rupert", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "49743b08-0f5d-4741-839c-b12933853780", "Cooper", "en", "AU", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "10bd4af4-825b-49b8-b8bd-0ca11865536e", "Rachel", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "d7bf7d75-64b7-4c1e-86c0-79d647366587", "Michelle", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "7d444628-dd13-442b-b687-71a6baf0c07e", "Joseph", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "25d7abcb-4d6d-4aca-adce-8a1c85620c8b", "Jessica", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "3e39e9a5-585c-4f5f-bac6-5e4905c51095", "Cole", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "263b9cc0-0d99-44e7-ae92-3d4ad5d2ad18", "Zanele", "en", "ZA", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "d1d9c946-7cfc-4378-85a4-07d09827cb7e", "Jolene", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "baf84392-fa95-4d44-8871-d32ee36b0e01", "Pieter", "en", "ZA", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "0ee8beaa-db49-4024-940d-c7ea09b590b3", "Morgan", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "692846ad-1a6b-49b8-bfc5-86421fd41a19", "Thandi", "en", "ZA", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "d79d2b77-9192-4e10-9407-5d43ca034803", "Siobhan", "en", "IE", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "e5d4c33a-d8f6-46e8-a10f-b5afecc35648", "Evie", "en", "GB", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "1ec736fa-db96-4eea-9299-235ce2cb7a0e", "Conor", "en", "IE", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "3c0f09d6-e0d7-499c-a594-70c5b7b93048", "Benedict", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "df89f42f-f285-4613-adbf-14eedcec4c9e", "Harrison", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "3d5ce2fb-e56c-42f0-9ed9-4662484063b4", "Toby", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", "Tessa", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "228fca29-3a0a-435c-8728-5cb483251068", "Kiefer", "en", None, "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30", "Linda", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "5cad89c9-d88a-4832-89fb-55f2f16d13d3", "Brandon", "en", None, "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "ec1e269e-9ca0-402f-8a18-58e0e022355a", "Ariana", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "66c6b81c-ddb7-4892-bdd5-19b5a7be38e7", "Dorothy", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "a7b8d8fa-f6e5-4908-900e-0c11d1d82519", "Joanie", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "999df508-4de5-40a7-8bd3-8c12f678c284", "Layla", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "26403c37-80c1-4a1a-8692-540551ca2ae5", "Marian", "en", None, "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "41468051-3a85-4b68-92ad-64add250d369", "Cory", "en", None, "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "c961b81c-a935-4c17-bfb3-ba2239de8c2f", "Kyle", "en", None, "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "694f9389-aac1-45b6-b726-9d9369183238", "Sarah", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "248be419-c632-4f23-adf1-5324ed7dbf1d", "Elizabeth", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "bf0a246a-8642-498a-9950-80c35e9276b5", "Sophie", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "57dcab65-68ac-45a6-8480-6c4c52ec1cd1", "Kira", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "78ab82d5-25be-4f7d-82b3-7ad64e5b85b2", "Savannah", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "03496517-369a-4db1-8236-3d3ae459ddf7", "Calypso", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "b7d50908-b17c-442d-ad8d-810c63997ed9", "Sierra", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "32b3f3c5-7171-46aa-abe7-b598964aa793", "Daisy", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "00a77add-48d5-4ef6-8157-71e5437b282d", "Callie", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "4af7c703-f2a9-45dd-a7fd-724cf7efc371", "Lila", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "156fb8d2-335b-4950-9cb3-a2d33befec77", "Sunny", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "8d8ce8c9-44a4-46c4-b10f-9a927b99a853", "Connie", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "c2ac25f9-ecc4-4f56-9095-651354df60c0", "Renee", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "5c42302c-194b-4d0c-ba1a-8cb485c84ab9", "Mary", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "146485fd-8736-41c7-88a8-7cdd0da34d84", "Tim", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "3b554273-4299-48b9-9aaf-eefd438e3941", "Simi", "en", "IN", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "71a7ad14-091c-4e8e-a314-022ece01c121", "Charlotte", "en", "GB", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "565510e8-6b45-45de-8758-13588fbaec73", "Ray", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "e3827ec5-697a-4b7c-9704-1a23041bbc51", "Dottie", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "98a34ef2-2140-4c28-9c71-663dc4dd7022", "Clyde", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "8f091740-3df1-4795-8bd9-dc62d88e5131", "Aurora", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "1463a4e1-56a1-4b41-b257-728d56e93605", "Hugo", "en", "GB", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "ed81fd13-2016-4a49-8fe3-c0d2761695fc", "Zack", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "34575e71-908f-4ab6-ab54-b08c95d6597d", "Joey", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "00967b2f-88a6-4a31-8153-110a92134b9f", "Asher", "en", "US", "masculine"
    ),
    CartesiaVoiceCatalogEntry(
        "5abd2130-146a-41b1-bcdb-974ea8e19f56", "Jo", "en", "US", "feminine"
    ),
    CartesiaVoiceCatalogEntry(
        "91b4cf29-5166-44eb-8054-30d40ecc8081", "Tina", "en", "US", "feminine"
    ),
)

DEFAULT_SUPER_AGENT_VOICE_IDS = tuple(
    voice.id
    for voice in CARTESIA_VOICE_CATALOG
    if voice.id != "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
)

_CATALOG_BY_ID = {voice.id: voice for voice in CARTESIA_VOICE_CATALOG}
_CATALOG_BY_NORMALIZED_NAME = {
    " ".join(voice.name.casefold().split()): voice for voice in CARTESIA_VOICE_CATALOG
}


def cartesia_voice_catalog_payload() -> list[dict[str, str | None]]:
    return [asdict(voice) for voice in CARTESIA_VOICE_CATALOG]


def cartesia_voice_for_id(voice_id: str) -> CartesiaVoiceCatalogEntry | None:
    return _CATALOG_BY_ID.get(voice_id)


def cartesia_voice_for_name(name: str) -> CartesiaVoiceCatalogEntry | None:
    normalized = " ".join(name.casefold().split())
    return _CATALOG_BY_NORMALIZED_NAME.get(normalized) if normalized else None
