import pytest

from company_dataset.description_rules import acceptable, describes_company


@pytest.mark.parametrize("name,text,ok", [
    ("Generate", "Generate is a leading sustainable infrastructure company that develops, builds, owns, and operates assets.", True),
    ("Karen Clark & Company", "Karen Clark & Company (KCC) is the innovation and technology leader in climate, weather, and catastrophe risk modeling.", True),
    ("COC Consulting", "COC, Continuity of Care, is a leading provider of mobile phlebotomy and imaging services in the US across nine states.", True),
    ("AJS", "AJS are one of the fastest growing and most innovative engineering businesses in the UK, specialising in automation.", True),
    ("Parspec", "Founded in 2021, Parspec is revolutionizing the materials supply chain for the construction industry by digitizing data.", True),
    ("Stellar Health", "At Stellar Health, we help primary care providers put patient health first with a mix of technology and analytics.", True),
    # Long and naming the company, but not saying what it is.
    ("Cometeer", "Capsules can be refrozen if not used immediately. Liquid capsules keep peak flavor for 24 hours. That's why Cometeer is great for travel.", False),
    ("Limelight Steel, Inc.", "Limelight Steel's Laser Furnace named one of the 50 greatest innovations of 2024 by a major technology magazine.", False),
    ("Carolina Complete Health Network", "Carolina Complete Health is providing advanced notice to its provider network of an upcoming update to UM requirements.", False),
    ("RadiantGraph, Inc", "RadiantGraph's platform delivered over 370% improvement in program adoption, with up to 2400% for the best performing cohorts.", False),
    ("Marathon Fusion", "Email us a resume with exactly two bullet points about your most impressive achievements and your interest in Marathon.", False),
    ("Fleetcraft", "News updates: Fleetcraft raises $3.2M to boost aircraft maintenance efficiency with AI, according to a report this week.", False),
    ("equipal", "Location: Hybrid - 8 days per month in office (London) Posted: July 2026 Status: Closed for applications at equipal.", False),
    ("Boam AI", "If these values match your own, Boam AI is a place where you'll feel supported to do your very best work every day.", False),
])
def test_describes_company(name, text, ok):
    assert describes_company(text, name) is ok


@pytest.mark.parametrize("method,text,name,ok", [
    ("board_page_meta", "BECOME A PART OF THE NIMBLE SUCCESS STORY NOW! Great things are happening here at this company every day.", "Nimble Credit Solutions", False),
    ("board_page_meta", "Making happier residents, one community at a time, all across the country and for many years to come.", "KMG Prestige", False),
    ("careers_page_meta", "We help people grow, and we think that you are going to love it here with all of us every day.", "Acme", False),
    ("homepage_paragraph", "Capsules can be refrozen if not used immediately, which is why Cometeer is a great choice for travel.", "Cometeer", False),
    ("about_page_paragraph", "MAP International is a Christ-centered global health organization guided by a vision of a healthier world.", "MAP International", True),
    # The vendor's own record and a homepage's meta are the company speaking for itself: other rules cover them.
    ("homepage_meta", "Don't settle for cheap swag. We offer remarkable goods branded with your company logo, and much more.", "Clove & Twine", True),
    ("provider_description", "We make great things for great customers, all around the world, every single day of the year.", "Acme", True),
    ("smartrecruiters_company_description", "Founded in Stuttgart in 1886, Bosch has a unique corporate culture based on trust and responsibility.", "Bosch", True),
    ("job_ad_about_section", "Our team builds tools that many of the largest firms in the world depend on to run every day.", "Acme", True),
])
def test_acceptable_depends_on_where_the_text_came_from(method, text, name, ok):
    assert acceptable(method, text, name) is ok


@pytest.mark.parametrize("name,text,ok", [
    ("Kirby Nagelhout Construction Company", "Kirby Nagelhout Construction Company (KNCC) was founded in Bend, Oregon in 1986 and has grown to become one of the largest contractors.", True),
    ("The Brooklyn Kindergarten Society", "The Brooklyn Kindergarten Society (BKS) is the original early childhood provider, helping families in Brooklyn for over 130 years.", True),
    ("K3 Solutions LLC", "Founded in May 2006, K3 Solutions LLC (K3) is a full-service management and information technology consulting firm.", True),
    ("Motley Fool Ventures", "Motley Fool Ventures is an early-stage venture capital fund powered by The Motley Fool's brand and investing approach.", True),
    ("Accelerate360", "At Accelerate360, we leverage our experience and innovation to provide a turnkey solution for retailers and brands.", True),
    ("Interfold", "At Interfold, we're re-imagining business lending for the 21st century with an AI platform for banks and customers.", True),
    ("Spacewell", "Spacewell, an international SaaS company, develops IoT solutions to simplify facility operations and reduce energy use.", True),
    ("Arcadia Science", "All work conducted by Arcadia Science is the intellectual property of Arcadia Science, and we publish selected work here.", False),
    ("Octant Bio", "Sri is a co-founder at Octant and an Associate Professor at UCLA in the Chemistry and Biochemistry Department.", False),
    ("Propel America", "Propel America's Medical Assistant pathway was one of several options she considered to invest her time and increase earnings.", False),
    ("Kobie Marketing", "Kobie LaunchPad 2026 is a university AI hackathon open to engineering and computer science students across India this year.", False),
    ("Gravitics, Inc", "Gravitics offers 3 Cigna medical plans with a generous company subsidy along with dental and vision plans for all staff.", False),
    ("Lumerate", "Lumerate is a fantastic place to work. Most individuals who choose to become Lumerites prefer to stay for the long haul.", False),
    ("Flow Traders", "Flow Traders offers a Global Rotation program open to candidates across all regions, because sharing knowledge is crucial.", False),
    ("Seaport Global Holdings LLC", "Seaport Global will host an enrichment program intended to provide a select group of undergraduates with exposure to professionals.", False),
    ("Merit America", "What made Merit America special for me was a shared experience and a coach who wants nothing more than your success.", False),
    ("mta", "Further, MTA provides quality engineering, assessment, and testing support on many different programs for its government customers.", False),
])
def test_paragraphs_need_the_company_as_grammatical_subject(name, text, ok):
    assert acceptable("homepage_paragraph", text, name) is ok


@pytest.mark.parametrize("name,text,ok", [
    ("Bosch", "Bosch was founded in Stuttgart in 1886 by Robert Bosch and has a unique corporate culture based on trust.", True),
    ("Alphabe Insight Inc", "At Biz Voice Boost, we believe that meaningful connections drive business success for the brands that we serve.", False),
    ("My NRMA", "At SIXT Australia, we're revolutionizing car rental. We believe it should be as exciting as a road trip for everyone.", False),
    ("krg technology inc", "Kumar.T | Resourcing Executive KRG Technologies, Inc., 25000 Avenue Stanford, Suite #243, Valencia, CA 91355 Direct : 661-558-0000", False),
])
def test_smartrecruiters_text_must_be_about_the_account_holder(name, text, ok):
    assert acceptable("smartrecruiters_company_description", text, name) is ok


@pytest.mark.parametrize("method", ["careers_page_paragraph", "careers_page_meta", "board_page_meta"])
def test_careers_and_board_text_is_never_used_even_when_it_reads_well(method):
    assert acceptable(method, "Acme is a leading provider of warehouse robots for regional distribution centers everywhere.", "Acme") is False


@pytest.mark.parametrize("name,text", [
    ("Boreala Management", "Are you interested in working in an organization where you can be yourself at Boreala Management and have an impact?"),
    ("Hive Logistics Inc", "Watch a Day in the Life of a Delivery Associate! Why Drive with Hive? Hive Logistics is an Amazon Delivery Partner in Lafayette."),
    ("Acme", "Join Acme today and help us build the best robots for warehouses all around the wide world."),
])
def test_smartrecruiters_text_opening_as_a_recruiting_pitch_is_rejected(name, text):
    assert acceptable("smartrecruiters_company_description", text, name) is False


@pytest.mark.parametrize("name, text", [
    ("Anton Paar", "You are... ... an expert in polymer markets at Anton Paar who can build consensus and collaboration."),
    ("Premier Logistics", "Premier Logistics is an Amazon Delivery Service Partner (DSP) and URGENTLY requires the services of Delivery Drivers to service routes."),
    ("Cloudnexa.com", "Cloudnexa is an Amazon Web Services (AWS) Premier Partner that is looking for an experienced and motivated Cloud support engineer."),
    ("Spie batignolles", "Envie de construire bien plus qu'une carrière ? Chez Spie batignolles, groupe indépendant fier de ses salariés."),
    ("McDonald's Österreich", "Als weltweiter Marktführer im Bereich der Systemgastronomie bieten wir dir bei McDonald's einen Teilzeitjob in deiner Nähe."),
    ("Forever", "✅ شركة الصبار Forever Living of America هي واحدة من أكبر الشركات"),
    ("Kynect Express", "Pay: From $22.50 per hour Job description: Kynect Express, LLC (KE) is hiring Delivery Associates for Amazon."),
    ("Courir", "Estamos à procura de Stockista apaixonados por sneakers para integrar a nossa equipa Courir."),
    ("Roni's Mac Bar", "Calling all mac-and-cheese enthusiasts! Looking for a fun and flexible job at Roni's Mac Bar?"),
    ("Carel Lurvink", "Hey sales professional! Maak impact bij industriële bedrijven in heel Nederland bij Carel Lurvink."),
    ("Kaufman & Broad", "Rejoignez l'aventure Kaufman & Broad ! Vous rêvez de rejoindre une entreprise."),
    ("EVORIEL", "LAMY, marque du Groupe Evoriel, recrute ! EVORIEL est un groupe indépendant de services immobiliers."),
])
def test_smartrecruiters_job_ad_text_is_not_a_company_description(name, text):
    assert not acceptable("smartrecruiters_company_description", text, name)


@pytest.mark.parametrize("name, text", [
    ("OceanaGold", "OceanaGold is a global intermediate gold and copper producer with more than 35 years of exploration and development."),
    ("Bank of China Luxembourg", "As a Luxembourg-based bank since 1979, together with the global presence of Bank of China Group, Bank of China Luxembourg has grown."),
    ("Nazarbayev University", "Назарбаев Университет - современный университет. Nazarbayev University находится в Астане."),
    ("UFF", "A l'origine du métier de conseiller en gestion de patrimoine en France, l'UFF est leader sur son marché depuis plus de 50 ans."),
])
def test_smartrecruiters_company_text_still_passes(name, text):
    assert acceptable("smartrecruiters_company_description", text, name)
